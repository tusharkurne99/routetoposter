"""The background map from OpenStreetMap: roads, rivers, lakes, glaciers and the sea.

Data comes from the Overpass API. The area is downloaded in tiles; each finished tile is cached, so
an interrupted download resumes where it stopped. How much is downloaded and drawn depends on the
area (see `download_level` and `roads_to_draw`).
"""
import json
import math
import time
import urllib.error
from urllib.parse import urlencode

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import linemerge, split, unary_union

from .errors import UserError
from .net import cache_path, cached_json, fetch_json, remember_area, saved_area

OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_BACKUPS = ["https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter"]
BUSY = {429, 502, 503, 504}
BUSY_PAUSES = (1, 2, 4, 8)  # the main server is often briefly busy ("504"); a quick retry usually works
QUICK_BUSY_PAUSES = (2, 5)  # the same for optional details: fewer retries
KM_PER_DEG = 111.32

# Road classes, most important first. OSM highway values not listed here are "default"
# (tracks, paths…).
ROAD_CLASSES = ["motorway", "primary", "secondary", "tertiary", "residential", "default"]
ROAD_CLASS = {
    "motorway": "motorway", "motorway_link": "motorway", "trunk": "motorway", "trunk_link": "motorway",
    "primary": "primary", "primary_link": "primary",
    "secondary": "secondary", "secondary_link": "secondary",
    "tertiary": "tertiary", "tertiary_link": "tertiary",
    "residential": "residential", "living_street": "residential",
    "unclassified": "residential", "service": "residential",
}
# Download levels, most detailed first: which roads and waterways are fetched.
LEVELS = ["high", "medium", "low", "minimal"]
HIGHWAY_FILTER = {
    "high": '["highway"]["highway"!~"^(steps|corridor|elevator|platform|bus_stop|proposed|construction|raceway|abandoned|razed)$"]',
    "medium": '["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street)(_link)?$"]',
    "low": '["highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]',
    "minimal": '["highway"~"^(motorway|trunk|primary|secondary)(_link)?$"]',
}
WATERWAY_FILTER = {"high": "^(river|stream|canal)$", "medium": "^(river|stream|canal)$",
                   "low": "^(river|canal)$", "minimal": "^river$"}
# The map is saved in squares of a fixed world grid, GRID_DEG degrees on a side, so any poster area
# reuses every square already saved: another size or orientation only downloads the squares it
# adds, and overlapping trips share squares. Sparser levels use bigger squares (each request costs
# a few seconds of server overhead), but not too big: an edge square can reach well past the
# poster. At latitude 30° a square is about 2,900 / 2,900 / 11,600 / 46,000 km².
GRID_DEG = {"high": 0.5, "medium": 0.5, "low": 1.0, "minimal": 2.0}
FEATURES_VERSION = 3  # part of every cache key; bump when `classify`'s output changes

# Line widths in points on a 12x16 in poster (scaled for other sizes), as in maptoposter.
LINE_WIDTH_PT = {"stream": 0.5, "river": 1.4, "default": 0.4, "residential": 0.5,
                 "tertiary": 0.8, "secondary": 1.1, "primary": 1.3, "motorway": 1.6}
# How much of the map surface road lines may cover before the map turns into a solid mat.
# Measured: Spiti with every road class covers 15% and looks right; Kerala with motorway + primary
# covers 15%, adding secondary roads takes it to 26%, which buries the route.
COVERAGE_BUDGET = 0.16
COVERAGE_FACTOR = {"less": 0.5, "auto": 1.0, "more": 2.0}  # the trip file's map_detail


Bbox = tuple[float, float, float, float]  # (south, west, north, east)


# ── which detail to fetch and draw ───────────────────────────────────────────────────────────────

def download_level(area_km2: float, map_detail: str) -> str:
    """How much to download for a poster covering `area_km2`. Over large areas minor roads would be
    thinner than a printed line anyway, and downloading them takes very long (Kerala at "medium":
    10+ minutes). `map_detail: more / less` moves one level up / down."""
    if area_km2 < 50_000:
        level = 0
    elif area_km2 < 80_000:
        level = 1
    elif area_km2 < 300_000:
        level = 2
    else:
        level = 3
    level += {"more": -1, "auto": 0, "less": 1}[map_detail]
    return LEVELS[min(max(level, 0), len(LEVELS) - 1)]


def roads_to_draw(features: dict, km_per_inch: float, map_area_in2: float, scale: float,
                  map_detail: str) -> list[str]:
    """The road classes to draw. Starting with the most important class, add classes while the share
    of the map covered by road lines (length × line width ÷ map area) stays within the budget.
    Motorways and primary roads are always drawn. `scale` is the line-width scale of this poster
    size (1 on a 12x16 in poster)."""
    budget = COVERAGE_BUDGET * COVERAGE_FACTOR[map_detail]
    chosen, covered = [], 0.0
    for cls in ROAD_CLASSES:
        length_in = sum(_length_deg(line) for line in features.get(cls, [])) * KM_PER_DEG / km_per_inch
        covered += length_in * LINE_WIDTH_PT[cls] * scale / 72 / map_area_in2
        if cls not in ("motorway", "primary") and covered > budget:
            break
        chosen.append(cls)
    return chosen


def _length_deg(line: list[list[float]]) -> float:
    """Approximate length of a [lon, lat] line in degrees of latitude."""
    c = math.cos(math.radians(line[0][1]))
    return sum(math.hypot((b[0] - a[0]) * c, b[1] - a[1]) for a, b in zip(line, line[1:]))


# ── downloading ──────────────────────────────────────────────────────────────────────────────────

def tiles_status(bbox: Bbox, level: str) -> tuple[int, int]:
    """(grid squares whose needed part is already saved, grid squares needed) for this area."""
    squares = grid_squares(bbox, level)
    return sum(_saved_square(sq, level, bbox) is not None for sq in squares), len(squares)


def fetch_features(bbox: Bbox, level: str, on_tile=None) -> dict[str, list]:
    """Roads, waterways, lakes and glaciers for the area, from the grid squares that overlap it
    (each downloaded once and saved). Returns {class name: [line or ring, …]} plus `named_rivers`
    and `named_lakes` for labels (see `classify`), limited to what touches `bbox`.
    `on_tile(done, total)` is called after each square."""
    squares = grid_squares(bbox, level)
    out: dict[str, list] = {}
    seen: set = set()
    for i, square in enumerate(squares, 1):
        part = _saved_square(square, level, bbox)
        if part is None:
            part = classify(overpass(_features_query(square, level))["elements"])
            _save_square(square, level, square, part)
        for key, items in part.items():
            for item in items:
                # Overpass returns every feature touching a square, so a road crossing a square's
                # edge arrives with both squares: keep one copy. Leave out what's off the poster.
                ident = (key, _identity(key, item))
                if ident not in seen and _touches(key, item, bbox):
                    seen.add(ident)
                    out.setdefault(key, []).append(item)
        if on_tile:
            on_tile(i, len(squares))
    return out


def grid_squares(bbox: Bbox, level: str) -> list[Bbox]:
    """The squares of the GRID_DEG grid that overlap the area, as (south, west, north, east)."""
    step = GRID_DEG[level]
    s, w, n, e = bbox
    rows = range(math.floor(s / step), math.ceil(n / step))
    cols = range(math.floor(w / step), math.ceil(e / step))
    return [(r * step, c * step, (r + 1) * step, (c + 1) * step) for r in rows for c in cols]


def _saved_square(square: Bbox, level: str, bbox: Bbox) -> dict | None:
    """The saved features of a square, if the saved part of it covers the part this area needs.
    (Normally the whole square is saved; map data converted from an older format may cover only
    part of a square.)"""
    path = cache_path("osm-grid", _square_key(square, level))
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    s, w, n, e = square
    S, W, N, E = bbox
    need = (max(s, S), max(w, W), min(n, N), min(e, E))  # the square's part inside the area
    cs, cw, cn, ce = saved["covers"]
    tol = 1e-6
    if cs <= need[0] + tol and cw <= need[1] + tol and cn >= need[2] - tol and ce >= need[3] - tol:
        return saved["features"]
    return None


def _save_square(square: Bbox, level: str, covers: Bbox, features: dict) -> None:
    """Save a square's features and which part of the square they are complete for."""
    path = cache_path("osm-grid", _square_key(square, level))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"covers": [float(v) for v in covers], "features": features}))


def _square_key(square: Bbox, level: str) -> list:
    return [[round(v, 4) for v in square], level, FEATURES_VERSION]


def _identity(key: str, item: list) -> tuple:
    """Enough to recognise the same feature arriving from two squares: its first and last points
    and its size (rivers and lakes: also the name)."""
    if key == "named_lakes":
        return tuple(item)
    if key == "named_rivers":
        name, pts = item
        return name, tuple(pts[0]), tuple(pts[-1]), len(pts)
    return tuple(item[0]), tuple(item[-1]), len(item)


def _touches(key: str, item: list, bbox: Bbox) -> bool:
    """Does the feature reach into the area? (Lakes for labels: is their centre inside?)"""
    s, w, n, e = bbox
    if key == "named_lakes":
        return s <= item[2] <= n and w <= item[1] <= e
    pts = item[1] if key == "named_rivers" else item
    return any(s <= lat <= n and w <= lon <= e for lon, lat in pts)


def _features_query(bbox: Bbox, level: str) -> str:
    b = ",".join(f"{v:.4f}" for v in bbox)
    return f"""[out:json][timeout:600];
(
 way{HIGHWAY_FILTER[level]}({b});
 way["waterway"~"{WATERWAY_FILTER[level]}"]({b});
 way["natural"~"^(water|glacier)$"]({b});
 relation["natural"~"^(water|glacier)$"]({b});
);
out geom qt;"""


def make_tiles(bbox: Bbox, target_km2: float) -> list[Bbox]:
    """Split the area into roughly square tiles of about `target_km2` each (for the sights query,
    which is too heavy to run over a whole large poster at once)."""
    s, w, n, e = bbox
    c = math.cos(math.radians((s + n) / 2))
    side = math.sqrt(target_km2)
    ny = max(1, math.ceil((n - s) * KM_PER_DEG / side))
    nx = max(1, math.ceil((e - w) * KM_PER_DEG * c / side))
    lats = [s + (n - s) * j / ny for j in range(ny)] + [n]
    lons = [w + (e - w) * i / nx for i in range(nx)] + [e]
    return [(lats[j], lons[i], lats[j + 1], lons[i + 1]) for j in range(ny) for i in range(nx)]


def overpass(query: str, patient: bool = True) -> dict:
    """Run an Overpass query. A busy main server is retried with short pauses; if it stays busy or
    can't be reached, the backup servers are tried.

    patient=True (the map itself): long timeouts, four retries when busy, every backup.
    patient=False (passes, sights, coastline): short timeouts, two retries when busy, one backup,
    so a struggling server costs at most about 2½ minutes before the poster goes ahead without
    that detail."""
    body = urlencode({"data": query}).encode()
    main_timeout, backup_timeout = (300, 120) if patient else (90, 60)
    pauses = BUSY_PAUSES if patient else QUICK_BUSY_PAUSES
    backups = OVERPASS_BACKUPS if patient else OVERPASS_BACKUPS[:1]
    errors = []
    for pause in (*pauses, None):
        try:
            return _complete(fetch_json(OVERPASS, body, timeout=main_timeout, retries=0))
        except urllib.error.HTTPError as e:
            if e.code not in BUSY:
                raise
            errors.append(f"{OVERPASS}: {e}")
            if pause is None:
                break
            time.sleep(pause)
        except OSError as e:  # unreachable, timed out (URLError and TimeoutError are OSErrors)
            errors.append(f"{OVERPASS}: {e}")
            break
    for url in backups:
        try:
            return _complete(fetch_json(url, body, timeout=backup_timeout, retries=0))
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise UserError("The OpenStreetMap servers are busy or unreachable; try again in a few minutes.\n  "
                    + "\n  ".join(errors))


def _complete(data: dict) -> dict:
    """Overpass can answer "200 OK" with a partial result and an error in `remark` (usually a
    timeout). Such a result must not be cached, so it's raised as an error."""
    remark = (data.get("remark") or "").strip()
    if "error" in remark.lower():
        raise UserError(f"OpenStreetMap's server ran out of time on part of the map ({remark}).\n"
                        "  Nothing incomplete was saved. Try again in a few minutes, or set map_detail: less.")
    return data


# ── turning Overpass elements into drawable shapes ──────────────────────────────────────────────

def classify(elements: list[dict]) -> dict[str, list]:
    """Sort raw Overpass elements into drawing classes: the road classes, "river", "stream", "lake",
    "glacier" (each a list of [[lon, lat], …]). Named rivers and lakes are also listed under
    "named_rivers" ([name, line]) and "named_lakes" ([name, centre lon, centre lat, width°, height°])
    for labels."""
    out: dict[str, list] = {c: [] for c in ROAD_CLASSES + ["river", "stream", "lake", "glacier"]}
    out["named_rivers"], out["named_lakes"] = [], []
    for el in elements:
        tags = el.get("tags", {})
        name = printable_name(tags)
        if el["type"] == "way":
            pts = _coords(el.get("geometry", []))
            if len(pts) < 2:
                continue
            if "highway" in tags:
                out[ROAD_CLASS.get(tags["highway"], "default")].append(pts)
            elif "waterway" in tags:
                out["river" if tags["waterway"] in ("river", "canal") else "stream"].append(pts)
                if tags["waterway"] == "river" and name:
                    out["named_rivers"].append([name, pts])
            elif tags.get("natural") == "water" and len(pts) >= 3:
                out["lake"].append(pts)
                if name:
                    out["named_lakes"].append(_lake_entry(name, pts))
            elif tags.get("natural") == "glacier" and len(pts) >= 3:
                out["glacier"].append(pts)
        elif el["type"] == "relation":
            key = "lake" if tags.get("natural") == "water" else "glacier"
            rings = join_rings([m for m in el.get("members", []) if m.get("role") == "outer" and m.get("geometry")])
            out[key].extend(rings)
            if key == "lake" and name and rings:
                out["named_lakes"].append(_lake_entry(name, max(rings, key=len)))
    return out


def _coords(geometry) -> list[list[float]]:
    return [[round(p["lon"], 5), round(p["lat"], 5)] for p in geometry if p]


def join_rings(members) -> list[list[list[float]]]:
    """Join a multipolygon's member ways into closed rings by matching their end points."""
    segments = [_coords(m["geometry"]) for m in members]
    rings = []
    while segments:
        ring = segments.pop(0)
        joined = True
        while ring and ring[0] != ring[-1] and joined:
            joined = False
            for i, seg in enumerate(segments):
                if seg[0] == ring[-1]:
                    ring = ring + seg[1:]
                elif seg[-1] == ring[-1]:
                    ring = ring + seg[::-1][1:]
                elif seg[-1] == ring[0]:
                    ring = seg + ring[1:]
                elif seg[0] == ring[0]:
                    ring = seg[::-1] + ring[1:]
                else:
                    continue
                segments.pop(i)
                joined = True
                break
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _lake_entry(name: str, ring: list[list[float]]) -> list:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return [name, (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2,
            max(lons) - min(lons), max(lats) - min(lats)]


# The poster fonts only cover Latin letters; a name in another script would print as empty boxes.
EXTRA_LATIN = set("–—‘’“”·•")


def is_latin(text: str) -> bool:
    return all(ord(ch) < 0x250 or ch in EXTRA_LATIN for ch in text)


def printable_name(tags: dict) -> str | None:
    """A name the poster can print: English, international, or the local name if it's in Latin script."""
    for key in ("name:en", "int_name", "name"):
        value = tags.get(key)
        if value and is_latin(value):
            return value
    return None


# ── the sea ──────────────────────────────────────────────────────────────────────────────────────

def fetch_sea(bbox: Bbox) -> list[list[list[list[float]]]]:
    """The sea inside the area, as polygons (see `sea_polygons`); empty when the area has no coast."""
    bbox = saved_area("coastline", bbox)
    b = ",".join(f"{v:.4f}" for v in bbox)
    lines = cached_json("coastline", [round(v, 3) for v in bbox], lambda: [
        _coords(el["geometry"]) for el in overpass(
            f'[out:json][timeout:300];way["natural"="coastline"]({b});out geom;', patient=False)["elements"]
        if el.get("geometry")])
    remember_area("coastline", bbox)
    return sea_polygons(lines, bbox)


def sea_polygons(coastlines: list[list[list[float]]], bbox: Bbox) -> list[list[list[list[float]]]]:
    """Cut the area along the coastline and keep the pieces that are sea. Each polygon is a list of
    rings of [lon, lat]: the outline first, then any islands as holes.

    OpenStreetMap draws every coastline with the land on its left and the sea on its right. So a
    piece of the area is sea when the points just to the right of the coastline segments on its
    edge fall inside it."""
    if not coastlines:
        return []
    s, w, n, e = bbox
    merged = linemerge([LineString(c) for c in coastlines if len(c) >= 2])
    lines = list(getattr(merged, "geoms", [merged]))
    pieces = split(box(w, s, e, n), unary_union(lines)).geoms
    return [[_ring(piece.exterior)] + [_ring(hole) for hole in piece.interiors]
            for piece in pieces if _is_sea(piece, lines)]


def _ring(ring) -> list[list[float]]:
    return [[x, y] for x, y in ring.coords]


def _is_sea(piece: Polygon, lines: list[LineString]) -> bool:
    """Vote over up to 5 coastline segments on the piece's edge: is the point to their right inside?"""
    edge = piece.boundary
    votes = 0
    for line in lines:
        coords = list(line.coords)
        for a, b in zip(coords, coords[1:]):
            mid = Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if edge.distance(mid) > 1e-7:
                continue  # this segment isn't on the piece's edge
            dx, dy = b[0] - a[0], b[1] - a[1]
            step = 1e-5 / (math.hypot(dx, dy) or 1.0)
            votes += 1 if piece.contains(Point(mid.x + dy * step, mid.y - dx * step)) else -1
            if abs(votes) >= 5:
                return votes > 0
    return votes > 0
