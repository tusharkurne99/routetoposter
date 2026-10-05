"""Details along the route: mountain passes, well-known sights ("auto sights") and altitudes.
Passes come from one small Overpass query; auto sights (only when switched on) from a heavier one
per tile. Altitudes come from the free Open-Meteo elevation API. Every lookup is cached.
"""
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np

from .mapdata import make_tiles, overpass, printable_name
from .net import cached_json, fetch_json, remember_area, saved_area
from .route import haversine_km

KM_PER_DEG = 111.32
POI_TILE_KM2 = 20_000  # auto sights: one query over a whole large poster made Overpass time out
POI_VERSION = 1  # part of the passes/sights cache keys; bump when their results change
PASS_MAX_KM = 0.3  # a pass must be this close to the route line
SIGHT_MAX_KM = 3.0  # auto sights: this close to the route
SIGHT_LIMIT = 8  # auto sights: at most this many
OPEN_METEO = "https://api.open-meteo.com/v1/elevation"
ELEVATION_BATCH = 100  # coordinates per Open-Meteo request (its limit)
CREST_TOLERANCE_M = 80  # the elevation grid can read a little under a pass's crest

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
    mapped as single points, so one small query covers even a very large area."""
    bbox = saved_area("passes", bbox)
    b = ",".join(f"{v:.4f}" for v in bbox)
    rows = cached_json("passes", [[round(v, 4) for v in bbox], POI_VERSION], lambda: [
        [printable_name(el.get("tags", {})), el["lon"], el["lat"], el.get("tags", {}).get("ele")]
        for el in overpass(f'[out:json][timeout:120];node["mountain_pass"="yes"]["name"]({b});out;', patient=False)["elements"]
        if printable_name(el.get("tags", {}))])
    remember_area("passes", bbox)
    return [Pass(name, lon, lat, _parse_ele(ele)) for name, lon, lat, ele in rows]


def fetch_sight_candidates(bbox: tuple[float, float, float, float]) -> list[Sight]:
    """Every candidate auto sight in the area (not yet filtered by the route). This query is heavy,
    so it runs per POI_TILE_KM2 tile, and only when `auto_sights` is on."""
    bbox = saved_area("sight-candidates", bbox)
    sights, seen = [], set()
    for tile in make_tiles(bbox, POI_TILE_KM2):
        rows = cached_json("sight-candidates", [[round(v, 4) for v in tile], POI_VERSION],
                           lambda tile=tile: _sight_tile(tile))
        for name, lon, lat, kind, famous in rows:
            key = (name, round(lon, 4), round(lat, 4))
            if key not in seen:  # the same place returned by two neighbouring tiles
                seen.add(key)
                sights.append(Sight(name, lon, lat, kind, famous=famous))
    remember_area("sight-candidates", bbox)
    return sights


def _sight_tile(bbox) -> list[list]:
    """Rows [name, lon, lat, kind, famous] for one tile."""
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


def passes_on_route(passes: list[Pass], route: list[Point]) -> list[Pass]:
    """Passes within PASS_MAX_KM of the route. The same pass is often mapped twice; one copy is kept
    (preferring the one with an altitude)."""
    passes = [p for p in passes if p.name.casefold() not in UNNAMED_PASSES]
    if not passes:
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


def auto_sights(candidates: list[Sight], route: list[Point], stops: list[Point]) -> list[Sight]:
    """Up to SIGHT_LIMIT sights within SIGHT_MAX_KM of the route: famous ones first, then the
    closest. Skips generic names, near-duplicates, and sights that would hide under a stop marker."""
    if not candidates:
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


def altitudes(stops: list[Point], route: list[Point], passes: list[Pass]) -> tuple[list[float], Pass | None, float]:
    """(stop altitudes rounded to 10 m, an extra "Highest point" marker or None, the trip's highest
    altitude). One request: the stops plus evenly spaced points along the route. Passes without an
    altitude in OSM get one here. If the highest route sample isn't at a pass or a stop (which show
    their altitude anyway), a "Highest point" marker is returned so the poster can show where it is."""
    samples = resample(route, max(ELEVATION_BATCH - len(stops), 20))
    heights = elevations(list(stops) + samples)
    stop_heights = [round(h, -1) for h in heights[:len(stops)]]
    route_heights = heights[len(stops):]

    missing = [p for p in passes if p.ele is None]
    for p, h in zip(missing, elevations([(p.lon, p.lat) for p in missing]) if missing else []):
        p.ele = round(h, -1)

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


def distance_to_route_km(points: list[Point], route: list[Point]) -> np.ndarray:
    """For each (lon, lat), the distance in km to the nearest point of the route line."""
    r = np.asarray(route, dtype=float)
    c = np.cos(np.radians(r[:, 1].mean()))
    xy = np.column_stack((r[:, 0] * c, r[:, 1])) * KM_PER_DEG
    a, b = xy[:-1], xy[1:]
    ab = b - a
    ab_len2 = np.maximum((ab ** 2).sum(axis=1), 1e-12)
    out = np.empty(len(points))
    for i, (lon, lat) in enumerate(points):
        q = np.array([lon * c, lat]) * KM_PER_DEG
        t = np.clip(((q - a) * ab).sum(axis=1) / ab_len2, 0, 1)  # nearest spot on each segment
        out[i] = np.hypot(*(a + ab * t[:, None] - q).T).min()
    return out
