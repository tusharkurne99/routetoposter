"""The background map from OpenStreetMap: roads, rivers, lakes, glaciers and the sea.

Data comes from the Overpass API. The area is downloaded in tiles; each finished tile is cached, so
an interrupted download resumes where it stopped. How much is downloaded and drawn depends on the
area (see `download_level` and `roads_to_draw`).

Each Overpass request costs ~12 s of server overhead even when the answer is empty, so the time goes
with the number of squares, not the amount of data. Two things keep it down: squares that are all
sea are not asked for (the sea is worked out first, from the coastline), and OVERPASS_SLOTS
squares are downloaded at once.
"""
import json
import math
import os
import socket
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, urlparse

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.ops import linemerge, split, unary_union

from .errors import UserError
from .net import NOTICE, CertificateProblem, cache_path, fetch_json, grid_rows, old_area_rows
from .net import grid_squares as world_grid_squares

OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_BACKUPS = ["https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter"]
# $ROUTETOPOSTER_OVERPASS: your own server list (comma-separated, tried in that order) instead of
# the ones above, e.g. a self-hosted Overpass. The public servers are all in Europe; the distance
# costs well under a second per request, so a different server only helps when it is less busy.
OVERPASS_ENV = "ROUTETOPOSTER_OVERPASS"
# Before each request the server must accept a connection within CONNECT_TIMEOUT seconds. A server
# that is down otherwise hangs every request for the whole read timeout (minutes); this way it is
# skipped for DOWN_FOR seconds and the next server is asked straight away.
CONNECT_TIMEOUT = 10
DOWN_FOR = 300
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
# Squares downloaded at the same time. overpass-api.de gives each address 2 slots; more requests at
# once only queue or get "429 Too Many Requests".
OVERPASS_SLOTS = 2
# The coastline is saved in squares of this many degrees (see net.grid_rows). Bigger than the map's
# squares: most of a coastline square is empty land or open sea, and each request costs the same
# server overhead. A sailing leg's search area usually fits in one or two squares.
COAST_GRID_DEG = 2.0
# A square is skipped as sea when less than this share of its part of the poster is land. Not 0:
# the sea polygons' edges and the square's edges meet with rounding noise.
SEA_SKIP_LAND_SHARE = 1e-6

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

def tiles_status(bbox: Bbox, level: str, sea: list | None = None) -> tuple[int, int, int]:
    """(grid squares whose needed part is already saved, grid squares needed, squares skipped as
    all sea) for this area. `sea`: fetch_sea's polygons for the area."""
    squares = grid_squares(bbox, level)
    land = land_squares(squares, bbox, sea)
    return sum(_saved_square(sq, level, bbox) is not None for sq in land), len(land), len(squares) - len(land)


def land_squares(squares: list[Bbox], bbox: Bbox, sea: list | None) -> list[Bbox]:
    """The squares whose part of the area isn't all sea. Without sea polygons (no coast, or the
    coastline couldn't be fetched) every square counts: better a slow poster than missing land.
    Islands are holes in the sea polygons, so a square with an island stays."""
    if not sea:
        return squares
    water = unary_union([Polygon(rings[0], rings[1:]) for rings in sea]).buffer(0)
    s, w, n, e = bbox
    out = []
    for sq in squares:
        need = box(max(sq[1], w), max(sq[0], s), min(sq[3], e), min(sq[2], n))
        if need.area == 0 or need.difference(water).area > SEA_SKIP_LAND_SHARE * need.area:
            out.append(sq)
    return out


def fetch_features(bbox: Bbox, level: str, on_tile=None, sea: list | None = None, notice=None) -> dict[str, list]:
    """Roads, waterways, lakes and glaciers for the area, from the grid squares that overlap it
    (each downloaded once and saved; squares that are all `sea` are skipped). Returns {class name:
    [line or ring, …]} plus `named_rivers` and `named_lakes` for labels (see `classify`), limited to
    what touches `bbox`. Missing squares are downloaded OVERPASS_SLOTS at a time;
    `on_tile(done, total)` is called after each download, `notice(text)` with what the server is
    doing (see `overpass`)."""
    squares = land_squares(grid_squares(bbox, level), bbox, sea)
    parts = {sq: _saved_square(sq, level, bbox) for sq in squares}
    missing = [sq for sq, part in parts.items() if part is None]

    def download(square: Bbox) -> dict:
        part = classify(overpass(_features_query(square, level), notice=notice)["elements"])
        _save_square(square, level, square, part)
        return part

    if missing:
        with ThreadPoolExecutor(OVERPASS_SLOTS) as pool:
            futures = {pool.submit(download, sq): sq for sq in missing}
            try:
                for i, future in enumerate(as_completed(futures), 1):
                    parts[futures[future]] = future.result()
                    if on_tile:
                        on_tile(i, len(missing))
            except BaseException:  # a failed square (or Ctrl-C): don't start the rest; finished ones are saved
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    out: dict[str, list] = {}
    seen: set = set()
    for square in squares:  # in grid order, so the result doesn't depend on which download finished first
        for key, items in parts[square].items():
            for item in items:
                # Overpass returns every feature touching a square, so a road crossing a square's
                # edge arrives with both squares: keep one copy. Leave out what's off the poster.
                ident = (key, _identity(key, item))
                if ident not in seen and _touches(key, item, bbox):
                    seen.add(ident)
                    out.setdefault(key, []).append(item)
    return out


def grid_squares(bbox: Bbox, level: str) -> list[Bbox]:
    """The squares of the GRID_DEG grid that overlap the area, as (south, west, north, east)."""
    return world_grid_squares(bbox, GRID_DEG[level])


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


def overpass(query: str, patient: bool = True, notice=None) -> dict:
    """Run an Overpass query. A busy main server is retried with short pauses; if it stays busy or
    can't be reached, the backup servers are tried. A server that doesn't accept a connection is
    skipped at once (`_reachable`). `notice(text)` is told what is happening ("waiting for …",
    "… is busy, retrying"), so a slow server doesn't look like a hang.

    patient=True (the map itself): long timeouts, four retries when busy, every backup.
    patient=False (passes, sights, coastline): short timeouts, two retries when busy, one backup,
    so a struggling server costs at most about 2½ minutes before the poster goes ahead without
    that detail."""
    say = notice or NOTICE.get() or (lambda text: None)
    body = urlencode({"data": query}).encode()
    main_timeout, backup_timeout = (300, 120) if patient else (90, 60)
    pauses = BUSY_PAUSES if patient else QUICK_BUSY_PAUSES
    main, *backups = overpass_servers()
    backups = backups if patient else backups[:1]
    errors = []
    for pause in (*pauses, None):
        if not _reachable(main):
            errors.append(f"{main}: doesn't accept connections")
            say(f"{_host(main)} isn't answering; trying another server")
            break
        try:
            say(f"waiting for {_host(main)}")
            return _complete(fetch_json(main, body, timeout=main_timeout, retries=0))
        except urllib.error.HTTPError as e:
            if e.code not in BUSY:
                raise
            errors.append(f"{main}: {e}")
            if pause is None:
                break
            say(f"{_host(main)} is busy ({e.code}); retrying in {pause}s")
            time.sleep(pause)
        except OSError as e:  # unreachable, timed out (URLError and TimeoutError are OSErrors)
            errors.append(f"{main}: {e}")
            break
    for url in backups:
        if not _reachable(url):
            errors.append(f"{url}: doesn't accept connections")
            continue
        try:
            say(f"waiting for {_host(url)} (backup server)")
            return _complete(fetch_json(url, body, timeout=backup_timeout, retries=0))
        except CertificateProblem:
            raise
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise UserError("The OpenStreetMap servers are busy or unreachable; try again in a few minutes.\n  "
                    + "\n  ".join(errors)
                    + f"\n  (To use another Overpass server, set {OVERPASS_ENV}=https://…/api/interpreter)")


def overpass_servers() -> list[str]:
    """The servers to ask, in order: $ROUTETOPOSTER_OVERPASS if set, else OVERPASS then the backups."""
    own = [u.strip() for u in os.environ.get(OVERPASS_ENV, "").split(",") if u.strip()]
    return own or [OVERPASS, *OVERPASS_BACKUPS]


_down: dict[str, float] = {}  # host -> when it last refused or ignored a connection
_down_lock = threading.Lock()


def _reachable(url: str) -> bool:
    """Does the server accept a connection within CONNECT_TIMEOUT? A failure is remembered for
    DOWN_FOR seconds, so the other squares (and the other download slot) don't wait for it again."""
    host = _host(url)
    with _down_lock:
        if time.time() - _down.get(host, -DOWN_FOR) < DOWN_FOR:
            return False
    parsed = urlparse(url)
    try:
        socket.create_connection((host, parsed.port or (443 if parsed.scheme == "https" else 80)),
                                 timeout=CONNECT_TIMEOUT).close()
        return True
    except OSError:
        with _down_lock:
            _down[host] = time.time()
        return False


def _host(url: str) -> str:
    return urlparse(url).hostname or url


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
    lines = old_area_rows("coastline", bbox, lambda area: [round(v, 3) for v in area])
    if lines is None:
        lines, seen = [], set()
        for line in grid_rows("coastline-grid", bbox, COAST_GRID_DEG, [], _coastline, OVERPASS_SLOTS):
            ident = (tuple(line[0]), tuple(line[-1]), len(line))
            if ident not in seen:  # a way crossing a square's edge comes back with both squares
                seen.add(ident)
                lines.append(line)
    return sea_polygons(lines, bbox)


def _coastline(square: Bbox) -> list[list[list[float]]]:
    b = ",".join(f"{v:.4f}" for v in square)
    return [_coords(el["geometry"]) for el in overpass(
        f'[out:json][timeout:300];way["natural"="coastline"]({b});out geom;', patient=False)["elements"]
        if el.get("geometry")]


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
    segments = _Segments(lines)
    return [[_ring(piece.exterior)] + [_ring(hole) for hole in piece.interiors]
            for piece in pieces if segments.right_is_inside(piece)]


def _ring(ring) -> list[list[float]]:
    return [[x, y] for x, y in ring.coords]


class _Segments:
    """Every coastline segment's midpoint and the point just to its right, in a spatial index.

    A coast with many islands (Galapagos: 776 islands, 164,000 points) cuts the area into hundreds
    of pieces; testing every segment against every piece took minutes. The index finds the few
    segments near a piece, and they are tested in batches until the vote is clear."""
    BATCH = 64

    def __init__(self, lines: list[LineString]):
        a = np.concatenate([np.asarray(line.coords)[:-1] for line in lines])
        b = np.concatenate([np.asarray(line.coords)[1:] for line in lines])
        d = b - a
        step = 1e-5 / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-12)[:, None]
        mid = (a + b) / 2
        self.mid = shapely.points(mid)
        self.right = mid + np.column_stack((d[:, 1], -d[:, 0])) * step
        self.tree = shapely.STRtree(self.mid)

    def right_is_inside(self, piece: Polygon) -> bool:
        """Vote over up to 5 coastline segments on the piece's edge: is the point to their right inside?"""
        edge = piece.boundary
        shapely.prepare(edge)
        shapely.prepare(piece)
        near = np.sort(self.tree.query(box(*piece.bounds)))
        votes = 0
        for i in range(0, len(near), self.BATCH):
            batch = near[i:i + self.BATCH]
            on_edge = batch[shapely.dwithin(edge, self.mid[batch], 1e-7)]  # segments that bound this piece
            for inside in shapely.contains_xy(piece, self.right[on_edge, 0], self.right[on_edge, 1]):
                votes += 1 if inside else -1
                if abs(votes) >= 5:
                    return votes > 0
        return votes > 0
