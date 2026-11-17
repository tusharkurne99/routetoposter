"""Details along the route: mountain passes, well-known sights ("auto sights") and altitudes.
Passes and peaks come from small Overpass queries, auto sights (only when switched on) from a heavier
one; all three per square of a fixed world grid (net.grid_rows), so a shifted poster area only asks
for the squares it adds. Altitudes come from the free Open-Meteo elevation API. Every lookup is cached.
"""
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np

from .mapdata import DOWN_FOR, LIGHT_TIMEOUT, OVERPASS_SLOTS, overpass, printable_name
from .net import CertificateProblem, cached_json, fetch_json, grid_rows, inside, notice, old_area_rows
from .route import haversine_km

KM_PER_DEG = 111.32
POI_GRID_DEG = 2.0  # passes and peaks are saved per square this size: few points, a light query
SIGHT_GRID_DEG = 1.0  # auto sights: a heavy query (one over a whole large poster made Overpass time out)
POI_VERSION = 1  # part of the passes/sights cache keys; bump when their results change
PASS_MAX_KM = 0.3  # a pass must be this close to the route line
SIGHT_MAX_KM = 3.0  # auto sights: this close to the route
SIGHT_LIMIT = 8  # auto sights: at most this many
OPEN_METEO = "https://api.open-meteo.com/v1/elevation"
ELEVATION_BATCH = 100  # coordinates per Open-Meteo request (its limit)
CREST_TOLERANCE_M = 80  # the elevation grid can read a little under a pass's crest
PEAK_MIN_M = 1000  # lower "peaks" are mostly hills on the plains
# Peaks come from Wikidata when OpenStreetMap's Overpass servers don't answer: free, no key.
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_PEAK_TYPES = ("wd:Q8502", "wd:Q207326", "wd:Q54050", "wd:Q8072")  # mountain, summit, hill, volcano

Point = tuple[float, float]  # (lon, lat)


@dataclass
class Pass:
    name: str
    lon: float
    lat: float
    ele: float | None  # metres


@dataclass
class Sight:
    """A sight drawn on the map: one of the user's sights_nearby, or an auto sight."""
    name: str
    lon: float
    lat: float
    kind: str  # one of trip.SIGHT_KINDS
    stop: tuple[float, float] | None = None  # (lon, lat) of the stop it belongs to (user sights only)
    famous: bool = False  # has a Wikipedia/Wikidata link (auto sights are ranked by this)


# ── passes and auto sights ───────────────────────────────────────────────────────────────────────

def fetch_passes(bbox: tuple[float, float, float, float]) -> list[Pass]:
    """Every named mountain pass in the area (not yet filtered by the route). Passes are few and
    mapped as single points, so the query is small even for a big square."""
    rows = old_area_rows("passes", bbox, lambda area: [[round(v, 4) for v in area], POI_VERSION])
    if rows is None:
        rows = grid_rows("passes-grid", bbox, POI_GRID_DEG, [POI_VERSION], _passes_in, OVERPASS_SLOTS)
    return [Pass(name, lon, lat, _parse_ele(ele)) for name, lon, lat, ele in _unique_inside(rows, bbox, 1)]


def _passes_in(square) -> list[list]:
    b = ",".join(f"{v:.4f}" for v in square)
    return [[printable_name(el.get("tags", {})), el["lon"], el["lat"], el.get("tags", {}).get("ele")]
            for el in overpass(f'[out:json][timeout:{LIGHT_TIMEOUT}];node["mountain_pass"="yes"]["name"]({b});out;', patient=False, light=True)["elements"]
            if printable_name(el.get("tags", {}))]


def fetch_peaks(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float]]:
    """(lon, lat, metres) of every mountain peak in the area that OpenStreetMap (or, failing that,
    Wikidata) gives an altitude of at least PEAK_MIN_M; the Pelennor Fields style draws mountain
    symbols on them."""
    rows = old_area_rows("peaks", bbox, lambda area: [[round(v, 4) for v in area], POI_VERSION])
    if rows is None:
        rows = grid_rows("peaks-grid", bbox, POI_GRID_DEG, [POI_VERSION], _peaks_in, OVERPASS_SLOTS)
    return [tuple(row) for row in _unique_inside(rows, bbox, 0)]


def _peaks_in(square) -> list[list]:
    """[lon, lat, metres] rows for one square: from OpenStreetMap, or Wikidata when no Overpass
    server answers. Wikidata knows far fewer peaks (a Costa Rica square: 7 against OSM's 388), but
    the big ones are there, and its answer is saved like OSM's, so the square isn't asked again."""
    b = ",".join(f"{v:.4f}" for v in square)
    if time.time() - _overpass_gave_up[0] < DOWN_FOR:  # failed for another square just now: don't wait again
        return _wikidata_peaks(square)
    try:
        elements = overpass(f'[out:json][timeout:{LIGHT_TIMEOUT}];node["natural"="peak"]["ele"]({b});out;',
                            patient=False, light=True)["elements"]
    except CertificateProblem:
        raise
    except Exception as e:
        _overpass_gave_up[0] = time.time()
        notice(f"OpenStreetMap didn't answer ({str(e).splitlines()[0]}); asking Wikidata")
        return _wikidata_peaks(square)
    return [[el["lon"], el["lat"], ele] for el in elements
            if (ele := _parse_ele(el.get("tags", {}).get("ele"))) is not None and ele >= PEAK_MIN_M]


_overpass_gave_up = [0.0]  # when no Overpass server answered a peaks query (a list: set from the download threads)


def _wikidata_peaks(square) -> list[list]:
    """[lon, lat, metres] of the mountains, summits and volcanoes Wikidata has in the square with an
    altitude of at least PEAK_MIN_M (normalised to metres: psn)."""
    s, w, n, e = square
    query = f"""SELECT ?item ?coord ?ele WHERE {{
  SERVICE wikibase:box {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({w} {s})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({e} {n})"^^geo:wktLiteral . }}
  VALUES ?type {{ {" ".join(WIKIDATA_PEAK_TYPES)} }}
  ?item wdt:P31 ?type ; p:P2044/psn:P2044/wikibase:quantityAmount ?ele .
  FILTER(?ele >= {PEAK_MIN_M})
}}"""
    data = fetch_json(f"{WIKIDATA_SPARQL}?{urlencode({'query': query, 'format': 'json'})}", timeout=LIGHT_TIMEOUT)
    rows, seen = [], set()
    for b in data["results"]["bindings"]:
        point = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", b["coord"]["value"])
        if point and b["item"]["value"] not in seen:  # an item with two types or altitudes comes back twice
            seen.add(b["item"]["value"])
            rows.append([float(point[1]), float(point[2]), float(b["ele"]["value"])])
    return rows


def fetch_sight_candidates(bbox: tuple[float, float, float, float]) -> list[Sight]:
    """Every candidate auto sight in the area (not yet filtered by the route). The query is heavy, so
    its squares are small (SIGHT_GRID_DEG), and it only runs when `auto_sights` is on."""
    rows = grid_rows("sight-candidates-grid", bbox, SIGHT_GRID_DEG, [POI_VERSION], _sight_tile, OVERPASS_SLOTS)
    return [Sight(name, lon, lat, kind, famous=famous) for name, lon, lat, kind, famous in _unique_inside(rows, bbox, 1)]


def _unique_inside(rows: list[list], bbox, lon_at: int) -> list[list]:
    """The rows whose (lon, lat), at row[lon_at] and row[lon_at + 1], lie in `bbox`, each place once:
    a ring (a monastery's outline) touching two squares comes back from both."""
    out, seen = [], set()
    for row in rows:
        lon, lat = row[lon_at], row[lon_at + 1]
        key = (*row[:lon_at], round(lon, 4), round(lat, 4))
        if key not in seen and inside(bbox, lon, lat):
            seen.add(key)
            out.append(row)
    return out


def _sight_tile(bbox) -> list[list]:
    """Rows [name, lon, lat, kind, famous] for one square."""
    b = ",".join(f"{v:.4f}" for v in bbox)
    query = f"""[out:json][timeout:240];
(
 nwr["amenity"="monastery"]["name"]({b});
 nwr["historic"="monastery"]["name"]({b});
 nwr["amenity"="place_of_worship"]["religion"="buddhist"]["name"]({b});
 nwr["amenity"="place_of_worship"]["name"]["wikidata"]({b});
 nwr["historic"~"^(castle|fort|palace)$"]["name"]({b});
 nwr["tourism"="viewpoint"]["name"]({b});
 nwr["natural"="waterfall"]["name"]({b});
 nwr["waterway"="waterfall"]["name"]({b});
 nwr["man_made"="lighthouse"]["name"]({b});
 nwr["tourism"~"^(museum|attraction)$"]["name"]["wikidata"]({b});
);
out center tags;"""
    rows = []
    for el in overpass(query, patient=False)["elements"]:
        tags = el.get("tags", {})
        name = printable_name(tags)
        pos = el.get("center") or el
        if name and "lat" in pos:
            rows.append([name, pos["lon"], pos["lat"], kind_from_tags(tags), "wikidata" in tags or "wikipedia" in tags])
    return rows


def kind_from_tags(tags: dict) -> str:
    """Which of trip.SIGHT_KINDS an OpenStreetMap place is, from its tags ("sight" if nothing fits)."""
    religion = tags.get("religion")
    if "monastery" in (tags.get("amenity"), tags.get("historic"), tags.get("building")) or religion == "buddhist":
        return "monastery"
    if tags.get("amenity") == "place_of_worship":
        return {"christian": "church", "muslim": "mosque"}.get(religion, "temple")
    checks = [
        ("fort", tags.get("historic") in ("castle", "fort")),
        ("palace", tags.get("historic") == "palace" or tags.get("castle_type") == "palace"),
        ("viewpoint", tags.get("tourism") == "viewpoint"),
        ("waterfall", "waterfall" in (tags.get("natural"), tags.get("waterway"))),
        ("hot_spring", tags.get("natural") == "hot_spring"),
        ("beach", tags.get("natural") == "beach"),
        ("lighthouse", tags.get("man_made") == "lighthouse"),
        ("peak", tags.get("natural") in ("peak", "volcano")),
        ("lake", tags.get("natural") == "water" or tags.get("water") == "lake"),
        ("dam", tags.get("waterway") == "dam" or tags.get("man_made") == "dam"),
        ("park", tags.get("leisure") in ("park", "nature_reserve") or tags.get("boundary") in ("national_park", "protected_area")),
        ("museum", tags.get("tourism") == "museum"),
    ]
    return next((kind for kind, matches in checks if matches), "sight")


def passes_on_route(passes: list[Pass], route: list[list[Point]]) -> list[Pass]:
    """Passes within PASS_MAX_KM of the route (its lines on land, Route.ground_lines). The same pass
    is often mapped twice; one copy is kept (preferring the one with an altitude)."""
    passes = [p for p in passes if p.name.casefold() not in UNNAMED_PASSES]
    if not passes or not route:
        return []
    dist = distance_to_route_km([(p.lon, p.lat) for p in passes], route)
    near: list[Pass] = []
    for p, d in zip(passes, dist):
        if d > PASS_MAX_KM:
            continue
        twin = next((o for o in near if haversine_km((o.lon, o.lat), (p.lon, p.lat)) < 1.5), None)
        if twin is None:
            near.append(p)
        elif twin.ele is None and p.ele is not None:
            near[near.index(twin)] = p
    return near


# "Names" that only say what it is: a label like "MOUNTAIN PASS" says nothing, so such passes are left out.
UNNAMED_PASSES = {"pass", "mountain pass", "la", "col", "saddle"}

# Names made only of these words ("Buddhist Temple") don't say which place it is.
GENERIC_WORDS = {"tibetan", "buddhist", "buddha", "monastery", "gompa", "gonpa", "temple", "old", "new",
                 "the", "of", "shri", "sri", "mandir", "fort", "viewpoint", "view", "point", "stupa", "chorten"}


def auto_sights(candidates: list[Sight], route: list[list[Point]], stops: list[Point]) -> list[Sight]:
    """Up to SIGHT_LIMIT sights within SIGHT_MAX_KM of the route (its lines on land): famous ones
    first, then the closest. Skips generic names, near-duplicates, and sights that would hide under
    a stop marker."""
    if not candidates or not route:
        return []
    dist = distance_to_route_km([(x.lon, x.lat) for x in candidates], route)
    ranked = sorted(((d, x) for d, x in zip(dist, candidates)
                     if d <= SIGHT_MAX_KM and (x.famous or not _generic(x.name))),
                    key=lambda dx: (not dx[1].famous, dx[0]))
    chosen: list[Sight] = []
    for _, x in ranked:
        here = (x.lon, x.lat)
        if any(haversine_km((o.lon, o.lat), here) < 1.0 for o in chosen):
            continue
        if any(haversine_km(st, here) < 0.5 for st in stops):
            continue
        chosen.append(x)
        if len(chosen) == SIGHT_LIMIT:
            break
    return chosen


def _generic(name: str) -> bool:
    words = [w.strip("()-.,'").lower() for w in name.split()]
    return all(w in GENERIC_WORDS for w in words if w)


def _parse_ele(value) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("m", "").strip())
    except ValueError:
        return None


# ── altitudes ────────────────────────────────────────────────────────────────────────────────────

def elevations(points: list[Point]) -> list[float]:
    """Altitude in metres for each (lon, lat), from Open-Meteo (a 90 m grid)."""
    out: list[float] = []
    for i in range(0, len(points), ELEVATION_BATCH):
        chunk = [(round(lon, 4), round(lat, 4)) for lon, lat in points[i:i + ELEVATION_BATCH]]
        query = urlencode({"latitude": ",".join(str(p[1]) for p in chunk),
                           "longitude": ",".join(str(p[0]) for p in chunk)})

        def produce(query=query):
            time.sleep(0.2)  # be gentle with a free service
            return fetch_json(f"{OPEN_METEO}?{query}", timeout=30)["elevation"]

        out.extend(cached_json("elevation", chunk, produce))
    return out


def altitudes(stops: list[Point], route: list[list[Point]], passes: list[Pass]) -> tuple[list[float], Pass | None, float]:
    """(stop altitudes rounded to 10 m, an extra "Highest point" marker or None, the trip's highest
    altitude). One request: the stops plus evenly spaced points along the route's lines on land.
    Passes without an altitude in OSM get one here. If the highest route sample isn't at a pass or a
    stop (which show their altitude anyway), a "Highest point" marker is returned so the poster can
    show where it is."""
    samples = resample_lines(route, max(ELEVATION_BATCH - len(stops), 20))
    heights = elevations(list(stops) + samples)
    stop_heights = [round(h, -1) for h in heights[:len(stops)]]
    route_heights = heights[len(stops):]

    missing = [p for p in passes if p.ele is None]
    for p, h in zip(missing, elevations([(p.lon, p.lat) for p in missing]) if missing else []):
        p.ele = round(h, -1)
    if not samples:  # flown or sailed all the way: the highest point is a stop
        return stop_heights, None, max(stop_heights)

    i = int(np.argmax(route_heights))
    top_m, top_point = round(route_heights[i], -1), samples[i]
    best_pass = max((p for p in passes if p.ele), key=lambda p: p.ele, default=None)
    if best_pass and best_pass.ele >= top_m - CREST_TOLERANCE_M:
        return stop_heights, None, best_pass.ele
    if any(haversine_km(point, top_point) < 2 for point in [(p.lon, p.lat) for p in passes] + list(stops)):
        return stop_heights, None, top_m  # already marked: at a pass or a stop
    return stop_heights, Pass("Highest point", top_point[0], top_point[1], top_m), top_m


def resample(coords: list[Point], n: int) -> list[Point]:
    """`n` points spaced evenly by distance along the line."""
    a = np.asarray(coords, dtype=float)
    c = np.cos(np.radians(a[:, 1].mean()))
    step = np.hypot(np.diff(a[:, 0]) * c, np.diff(a[:, 1])) * KM_PER_DEG
    dist = np.concatenate(([0.0], np.cumsum(step)))
    targets = np.linspace(0, dist[-1], n)
    return list(zip(np.interp(targets, dist, a[:, 0]).tolist(), np.interp(targets, dist, a[:, 1]).tolist()))


def resample_lines(lines: list[list[Point]], n: int) -> list[Point]:
    """About `n` points spread over several lines in proportion to their lengths (at least 2 each)."""
    lengths = [_length_km(line) for line in lines]
    total = sum(lengths) or 1
    return [p for line, km in zip(lines, lengths) for p in resample(line, max(2, round(n * km / total)))]


def _length_km(coords: list[Point]) -> float:
    a = np.asarray(coords, dtype=float)
    c = np.cos(np.radians(a[:, 1].mean()))
    return float(np.hypot(np.diff(a[:, 0]) * c, np.diff(a[:, 1])).sum() * KM_PER_DEG)


def distance_to_route_km(points: list[Point], route: list[list[Point]]) -> np.ndarray:
    """For each (lon, lat), the distance in km to the nearest point of the route's lines."""
    r = np.concatenate([np.asarray(line, dtype=float) for line in route])
    c = np.cos(np.radians(r[:, 1].mean()))
    a, b = [], []
    for line in route:  # each line's own pieces; no piece joins the end of one line to the next
        xy = np.column_stack((np.asarray(line)[:, 0] * c, np.asarray(line)[:, 1])) * KM_PER_DEG
        a.append(xy[:-1])
        b.append(xy[1:])
    a, b = np.concatenate(a), np.concatenate(b)
    ab = b - a
    ab_len2 = np.maximum((ab ** 2).sum(axis=1), 1e-12)
    out = np.empty(len(points))
    for i, (lon, lat) in enumerate(points):
        q = np.array([lon * c, lat]) * KM_PER_DEG
        t = np.clip(((q - a) * ab).sum(axis=1) / ab_len2, 0, 1)  # nearest spot on each segment
        out[i] = np.hypot(*(a + ab * t[:, None] - q).T).min()
    return out
