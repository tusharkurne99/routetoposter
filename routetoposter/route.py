"""The road line between the stops.

Order of attempts:
1. OSRM, the whole trip in one request (fast, gives every leg's distance and time).
2. If OSRM fails, or had to move a stop more than SNAP_LIMIT_M to reach a road it can use, each leg
   is routed on its own: OSRM, then Valhalla, then a straight line, each with a warning.
`route: straight` in the trip file skips routing and joins the stops with straight lines.
"""
import json
import math
from dataclasses import dataclass, field

from .net import cached_json, fetch_json
from .trip import Stop

OSRM = "https://router.project-osrm.org/route/v1/driving/"
VALHALLA = "https://valhalla1.openstreetmap.de/route"
# A router "snaps" each stop to the nearest road it can use. Moving a stop further than this means
# the router couldn't use the roads there (seen in practice: Bengaluru moved 168 km), so the result
# can't be trusted.
SNAP_LIMIT_M = 5000
# A leg longer than this many times the straight-line distance (and over DETOUR_MIN_KM) is flagged.
# Mountain roads wind, but rarely more than ~2.5x; Pangong Tso → Rezang La came out at 12x.
DETOUR_FACTOR = 4
DETOUR_MIN_KM = 50

Point = tuple[float, float]  # (lon, lat), the order GeoJSON and the routers use


@dataclass
class Leg:
    start: str  # stop names
    end: str
    km: float
    hours: float | None  # None for a straight line


@dataclass
class Route:
    coords: list[Point]  # the whole line, start to finish
    legs: list[Leg]
    warnings: list[str] = field(default_factory=list)

    @property
    def km(self) -> float:
        return sum(leg.km for leg in self.legs)


class RoutingFailed(Exception):
    pass


def route_trip(stops: list[Stop], mode: str) -> Route:
    """The route through `stops` in order. `mode` is the trip file's `route`: driving or straight."""
    if mode == "straight":
        return Route([(s.lon, s.lat) for s in stops], [_straight_leg(a, b) for a, b in zip(stops, stops[1:])])
    try:
        return _osrm_whole_trip(stops)
    except RoutingFailed as e:
        note = f"{e} Routed each leg on its own instead."

    coords: list[Point] = []
    legs: list[Leg] = []
    warnings = [note]
    for a, b in zip(stops, stops[1:]):
        leg_coords, leg = _one_leg(a, b, warnings)
        coords.extend(leg_coords if not coords else leg_coords[1:])  # legs share their end/start point
        legs.append(leg)
    return Route(coords, legs, warnings)


def _osrm_whole_trip(stops: list[Stop]) -> Route:
    coords, leg_stats = _osrm(stops)
    legs = [Leg(a.name, b.name, km, hours) for (a, b), (km, hours) in zip(zip(stops, stops[1:]), leg_stats)]
    return Route(coords, legs)


def _osrm(stops: list[Stop]) -> tuple[list[Point], list[tuple[float, float]]]:
    """(line, [(km, hours) per leg]) from OSRM. Raises RoutingFailed when OSRM has no route or had to
    move a stop too far."""
    points = ";".join(f"{s.lon:.6f},{s.lat:.6f}" for s in stops)
    url = f"{OSRM}{points}?overview=full&geometries=geojson"
    try:
        data = cached_json("osrm", url, lambda: fetch_json(url, timeout=60))
    except Exception as e:
        raise RoutingFailed(f"The OSRM router didn't answer ({e}).")
    if data.get("code") != "Ok":
        raise RoutingFailed(f"OSRM found no route ({data.get('message') or data.get('code')}).")
    moved = [s.name for s, w in zip(stops, data["waypoints"]) if w.get("distance", 0) > SNAP_LIMIT_M]
    if moved:
        raise RoutingFailed(f"OSRM couldn't use the roads at {', '.join(dict.fromkeys(moved))}.")
    route = data["routes"][0]
    legs = [(leg["distance"] / 1000, leg["duration"] / 3600) for leg in route["legs"]]
    return [tuple(c) for c in route["geometry"]["coordinates"]], legs


def _one_leg(a: Stop, b: Stop, warnings: list[str]) -> tuple[list[Point], Leg]:
    """One leg: OSRM, else Valhalla, else a straight line. Adds a warning for a straight line."""
    try:
        coords, [(km, hours)] = _osrm([a, b])
        return coords, Leg(a.name, b.name, km, hours)
    except RoutingFailed:
        pass
    try:
        coords, km, hours = _valhalla(a, b)
        return coords, Leg(a.name, b.name, km, hours)
    except Exception:
        pass
    warnings.append(f"No road route found from {a.name} to {b.name}; drawn as a straight line.")
    return [(a.lon, a.lat), (b.lon, b.lat)], _straight_leg(a, b)


def _valhalla(a: Stop, b: Stop) -> tuple[list[Point], float, float]:
    """(line, km, hours) for one leg from the Valhalla router."""
    body = {"locations": [{"lat": a.lat, "lon": a.lon}, {"lat": b.lat, "lon": b.lon}],
            "costing": "auto", "units": "kilometers"}
    data = cached_json("valhalla", body, lambda: fetch_json(
        VALHALLA, json.dumps(body).encode(), timeout=60, content_type="application/json"))
    if "trip" not in data:
        raise RoutingFailed(data.get("error", "no route"))
    trip = data["trip"]
    return decode_polyline6(trip["legs"][0]["shape"]), trip["summary"]["length"], trip["summary"]["time"] / 3600


def _straight_leg(a: Stop, b: Stop) -> Leg:
    return Leg(a.name, b.name, haversine_km((a.lon, a.lat), (b.lon, b.lat)), None)


def decode_polyline6(encoded: str) -> list[Point]:
    """Valhalla's encoded line (Google's polyline format, 6 decimals) -> [(lon, lat), …]."""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for is_lon in (False, True):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lon / 1e6, lat / 1e6))
    return coords


def haversine_km(a: Point, b: Point) -> float:
    """Distance in km between two (lon, lat) points along the Earth's surface."""
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def describe(route: Route, stops: list[Stop]) -> list[str]:
    """The lines `new` prints and writes into the trip file: one per leg, then the total, then
    warnings. A leg much longer by road than in a straight line (DETOUR_FACTOR) is flagged: the
    router probably doesn't know a road that exists, as near borders where OpenStreetMap's roads
    are often incomplete."""
    width = max(len(f"{leg.start} → {leg.end}") for leg in route.legs)
    lines, detours = [], []
    for leg, (a, b) in zip(route.legs, zip(stops, stops[1:])):
        time = f"~{int(leg.hours)}h {round(leg.hours % 1 * 60):02d}m" if leg.hours is not None else "straight line"
        straight = haversine_km((a.lon, a.lat), (b.lon, b.lat))
        detour = leg.km > DETOUR_MIN_KM and leg.km > DETOUR_FACTOR * straight
        lines.append(f"{leg.start + ' → ' + leg.end:<{width}}  {leg.km:6,.0f} km   {time}" + ("   ⚠ see below" if detour else ""))
        if detour:
            detours.append(f"{leg.start} → {leg.end}: {leg.km:,.0f} km by road but only {straight:,.0f} km apart. The router "
                           "probably doesn't know the road Google uses. Compare with Google Maps; if it's wrong, "
                           "pick a nearby stop on a main road, or set route: straight.")
    loop = haversine_km((stops[0].lon, stops[0].lat), (stops[-1].lon, stops[-1].lat)) < 1
    shape = f"loop (starts and ends in {stops[0].name})" if loop else f"{stops[0].name} to {stops[-1].name}"
    lines.append(f"Total {route.km:,.0f} km · {shape}")
    lines += [f"⚠ {w}" for w in route.warnings + detours]
    return lines
