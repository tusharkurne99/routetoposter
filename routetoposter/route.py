"""The road line between the stops.

Order of attempts:
1. OSRM, the whole trip in one request (fast, gives every leg's distance and time).
2. If OSRM fails, or had to move a stop more than SNAP_LIMIT_M to reach a road it can use, each leg
   is routed on its own: OSRM, then Valhalla, then a straight line, each with a warning.
`route: straight` in the trip file skips routing and joins the stops with straight lines.

A stop's own `route` (how you got there) overrides the trip's for that leg. The trip is cut into runs
of legs with the same mode; driving runs are routed as above, the others need no router: `flying` is
an arc (flight_arc), `sailing` goes round the land (sailing.py, from the coastline) or, if that
fails, a straight line from port to port with a warning (via stops can steer it).
Each run becomes one Segment, so render.py can draw each mode in its own line style.
"""
import json
import math
from dataclasses import dataclass, field

from .net import CertificateProblem, cached_json, fetch_json, notice
from .sailing import sea_route
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

# A flight's arc bows out sideways by this share of its length at the middle. Airline maps do this;
# it also tells a flight apart from a straight-line leg at a glance.
ARC_BEND = 0.15
ARC_POINTS = 48
# The poster is fitted to include this much of each leg to an off-map stop (a share of the on-map
# trip's size), so the line runs a little way across the map before it leaves, instead of off the edge at once.
OFF_MAP_LEAD = 0.25
GROUND = ("driving", "straight")  # modes on land: passes, sights and altitudes are looked for along these

Point = tuple[float, float]  # (lon, lat), the order GeoJSON and the routers use


@dataclass
class Leg:
    start: str  # stop names
    end: str
    km: float
    hours: float | None  # None for a straight line, a flight or a sea crossing
    mode: str = "driving"  # trip.ROUTE_MODES


@dataclass
class Segment:
    """A stretch of the route travelled one way (consecutive legs with the same mode)."""
    mode: str
    coords: list[Point]
    off_map: bool = False  # a leg to or from an `off_map` stop: not fitted on the poster, runs off its edge
    ends_on_map: tuple[bool, bool] = (True, True)  # are its first and last points on-map stops?


@dataclass
class Route:
    coords: list[Point]  # the whole line, start to finish (fitting the map, keeping labels off it)
    legs: list[Leg]
    warnings: list[str] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)  # the same line, cut where the mode changes

    def __post_init__(self):
        if not self.segments and self.coords:
            self.segments = [Segment("driving", self.coords)]

    @property
    def km(self) -> float:
        return sum(leg.km for leg in self.legs)

    def ground_lines(self) -> list[list[Point]]:
        """The stretches on land (and on the map). A flight passes over mountains it doesn't cross,
        so passes, sights and the highest point are only looked for along these."""
        return [seg.coords for seg in self.segments if seg.mode in GROUND and not seg.off_map]

    def fit_coords(self) -> list[Point]:
        """What the poster is fitted to: everything but the legs to off-map stops, plus the first
        OFF_MAP_LEAD of each of those legs, so the map reaches a little toward where they go."""
        on_map = [p for seg in self.segments if not seg.off_map for p in seg.coords]
        if not on_map:
            return self.coords
        lons, lats = [p[0] for p in on_map], [p[1] for p in on_map]
        size = max(haversine_km((min(lons), min(lats)), (max(lons), min(lats))),
                   haversine_km((min(lons), min(lats)), (min(lons), max(lats))))
        lead = [_along(line, OFF_MAP_LEAD * size)
                for seg in self.segments if seg.off_map
                for line, on in ((seg.coords, seg.ends_on_map[0]), (seg.coords[::-1], seg.ends_on_map[1])) if on]
        return on_map + lead

    def add(self, other: "Route") -> None:
        """Append `other`, which starts where this route ends."""
        self.coords.extend(other.coords if not self.coords else other.coords[1:])
        self.legs += other.legs
        self.warnings += other.warnings
        self.segments += other.segments


class RoutingFailed(Exception):
    pass


def route_trip(stops: list[Stop], mode: str) -> Route:
    """The route through `stops` in order. `mode` is the trip file's `route`; a stop's own `route`
    overrides it for the leg that arrives there."""
    # A leg is (mode, off the map?); a leg to or from an off_map stop gets a Segment of its own.
    kinds = [(b.route or mode, a.off_map or b.off_map) for a, b in zip(stops, stops[1:])]
    route = Route([], [])
    start = 0
    while start < len(kinds):
        end = start + 1  # legs start..end-1 are the same kind
        while end < len(kinds) and kinds[end] == kinds[start]:
            end += 1
        run = _route_run(stops[start:end + 1], kinds[start][0])
        for seg in run.segments:
            seg.off_map = kinds[start][1]
            seg.ends_on_map = (not stops[start].off_map, not stops[end].off_map)
        route.add(run)
        start = end
    return route


def _route_run(stops: list[Stop], mode: str) -> Route:
    """The route through `stops`, all travelled by `mode`, as one Segment."""
    pairs = list(zip(stops, stops[1:]))
    if mode == "sailing":
        return _sail(stops)
    if mode == "flying":
        coords: list[Point] = [(stops[0].lon, stops[0].lat)]
        for a, b in pairs:
            coords += flight_arc(a, b)[1:]
    elif mode == "driving":
        return _drive(stops)  # one "driving" Segment (Route.__post_init__)
    else:
        coords = [(s.lon, s.lat) for s in stops]
    return Route(coords, [_straight_leg(a, b, mode) for a, b in pairs], segments=[Segment(mode, coords)])


def _sail(stops: list[Stop]) -> Route:
    """Each sea crossing round the land (sailing.sea_route); a straight line with a warning where
    that can't be worked out."""
    from .mapdata import fetch_sea  # mapdata is heavy (shapely) and only needed here

    coords: list[Point] = [(stops[0].lon, stops[0].lat)]
    legs, warnings = [], []
    for a, b in zip(stops, stops[1:]):
        try:
            notice(f"sailing {a.name} → {b.name}: finding a way round the land")
            line = sea_route(a, b, fetch_sea)
            problem = "couldn't find a way round the land"
        except CertificateProblem:
            raise
        except Exception as e:  # the coastline couldn't be downloaded
            line, problem = None, f"couldn't get the coastline ({str(e).splitlines()[0] if str(e) else type(e).__name__})"
        if line is None:
            line = [(a.lon, a.lat), (b.lon, b.lat)]
            warnings.append(f"Sailing {a.name} → {b.name}: {problem}; drawn straight. Add via: true stops at sea to steer it.")
        coords += line[1:]
        legs.append(Leg(a.name, b.name, sum(haversine_km(p, q) for p, q in zip(line, line[1:])), None, "sailing"))
    return Route(coords, legs, warnings, [Segment("sailing", coords)])


def _drive(stops: list[Stop]) -> Route:
    legs = len(stops) - 1
    notice(f"OSRM: {stops[0].name} → {stops[-1].name}" + (f", {legs} legs at once" if legs > 1 else ""))
    try:
        return _osrm_whole_trip(stops)
    except RoutingFailed as e:
        note = f"{e} Routed each leg on its own instead."

    coords: list[Point] = []
    legs: list[Leg] = []
    warnings = [note]
    for i, (a, b) in enumerate(zip(stops, stops[1:]), 1):
        notice(f"leg {i}/{len(stops) - 1}: {a.name} → {b.name}")
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
    except CertificateProblem:
        raise
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
        notice(f"{a.name} → {b.name}: OSRM has no road; asking Valhalla")
        coords, km, hours = _valhalla(a, b)
        return coords, Leg(a.name, b.name, km, hours)
    except CertificateProblem:
        raise
    except Exception:
        pass
    warnings.append(f"No road route found from {a.name} to {b.name}; drawn as a straight line. If you flew or "
                    f"took a boat, set route: flying or route: sailing on {b.name}.")
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


def _along(line: list[Point], km: float) -> Point:
    """The point `km` along the line from its start (its end, if the line is shorter)."""
    for p, q in zip(line, line[1:]):
        step = haversine_km(p, q)
        if step >= km:
            f = km / step if step else 0
            return p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f
        km -= step
    return line[-1]


def _straight_leg(a: Stop, b: Stop, mode: str = "straight") -> Leg:
    return Leg(a.name, b.name, haversine_km((a.lon, a.lat), (b.lon, b.lat)), None, mode)


def flight_arc(a: Stop, b: Stop) -> list[Point]:
    """A flight as a curve (a quadratic Bézier) bowing ARC_BEND of its length out to the left of the
    way flown, so a flight there and back draws two arcs rather than one line twice. Worked out with
    longitude shrunk by cos(latitude), as in layout.Frame, so it looks round on the poster."""
    c = math.cos(math.radians((a.lat + b.lat) / 2))
    x0, y0, x1, y1 = a.lon * c, a.lat, b.lon * c, b.lat
    # The middle of a quadratic Bézier lies halfway to its control point, hence 2 * ARC_BEND.
    cx, cy = (x0 + x1) / 2 - 2 * ARC_BEND * (y1 - y0), (y0 + y1) / 2 + 2 * ARC_BEND * (x1 - x0)
    points = []
    for i in range(ARC_POINTS + 1):
        t = i / ARC_POINTS
        x = (1 - t) ** 2 * x0 + 2 * t * (1 - t) * cx + t ** 2 * x1
        y = (1 - t) ** 2 * y0 + 2 * t * (1 - t) * cy + t ** 2 * y1
        points.append((x / c, y))
    return points


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


HOW = {"driving": "by road", "straight": "straight line", "flying": "flight", "sailing": "by boat"}


def describe(route: Route, stops: list[Stop]) -> list[str]:
    """The lines `new` prints and writes into the trip file: one per leg, then the total, then
    warnings. A leg much longer by road than in a straight line (DETOUR_FACTOR) is flagged: the
    router probably doesn't know a road that exists, as near borders where OpenStreetMap's roads
    are often incomplete."""
    width = max(len(f"{leg.start} → {leg.end}") for leg in route.legs)
    lines, detours = [], []
    for leg, (a, b) in zip(route.legs, zip(stops, stops[1:])):
        time = f"~{int(leg.hours)}h {round(leg.hours % 1 * 60):02d}m" if leg.hours is not None else HOW[leg.mode]
        straight = haversine_km((a.lon, a.lat), (b.lon, b.lat))
        detour = leg.km > DETOUR_MIN_KM and leg.km > DETOUR_FACTOR * straight
        lines.append(f"{leg.start + ' → ' + leg.end:<{width}}  {leg.km:6,.0f} km   {time}" + ("   ⚠ see below" if detour else ""))
        if detour:
            detours.append(f"{leg.start} → {leg.end}: {leg.km:,.0f} km by road but only {straight:,.0f} km apart. The router "
                           "probably doesn't know the road Google uses. Compare with Google Maps; if it's wrong, "
                           "pick a nearby stop on a main road, or set route: straight.")
    loop = haversine_km((stops[0].lon, stops[0].lat), (stops[-1].lon, stops[-1].lat)) < 1
    shape = f"loop (starts and ends in {stops[0].name})" if loop else f"{stops[0].name} to {stops[-1].name}"
    by_mode: dict[str, float] = {}
    for leg in route.legs:
        by_mode[HOW[leg.mode]] = by_mode.get(HOW[leg.mode], 0) + leg.km
    split = f" ({', '.join(f'{km:,.0f} km {how}' for how, km in by_mode.items())})" if len(by_mode) > 1 else ""
    lines.append(f"Total {route.km:,.0f} km{split} · {shape}")
    lines += [f"⚠ {w}" for w in route.warnings + detours]
    return lines
