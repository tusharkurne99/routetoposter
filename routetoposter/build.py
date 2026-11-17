"""Turn a checked Trip into a Poster: route, frame, map data, details, text. Prints what it does.

This is the only module that calls the others in order; render.py then only draws.
"""
import shutil
import sys
import threading
import time
from contextlib import contextmanager

from . import units
from .extras import altitudes, auto_sights, fetch_passes, fetch_peaks, fetch_sight_candidates, passes_on_route
from .layout import Frame, bleed_inches, fit, poster_inches
from .mapdata import download_level, fetch_features, fetch_sea, roads_to_draw, tiles_status
from .net import NOTICE
from .pelennor_fields import BorderText
from .render import SEP, Poster, PosterText, is_loop
from .route import Route, haversine_km, route_trip
from .runes import number_words
from .sights import Found, locate_sights
from .trip import Trip

MAP_SHARE = 0.75  # roughly the share of the poster that shows map (the rest is the title block)
STOP_PASS_KM = 2.0  # a pass this close to a stop is that stop


def build_poster(trip: Trip) -> tuple[Poster, list[Found]]:
    """Everything needed to draw `trip`, plus where each sights_nearby entry was found. Named
    mountain peaks are looked up for every theme, though only the Pelennor Fields style draws them:
    the lookup is small, and then switching to that theme later downloads nothing."""
    print("1/4  Route")
    with looking_up("the route"):
        route = route_trip(trip.stops, trip.route)
    for warning in route.warnings:
        print(f"     ⚠ {warning}")
    shown = units.distance(route.on_map_km, trip.units).lower()
    if route.on_map_km < route.km:
        print(f"     {shown} on the map ({units.distance(route.km, trip.units).lower()} with the legs to off-map stops)")
    else:
        print(f"     {shown}")

    width, height = poster_inches(trip)
    frame = fit(route.fit_coords(), width, height)
    features, sea, roads = map_layers(trip, frame)

    print("3/4  Passes, sights and altitudes")
    stop_points = [(s.lon, s.lat) for s in trip.stops]
    ground = route.ground_lines()
    passes = []
    if trip.show.passes:
        passes = passes_on_route(optional("mountain passes", lambda: fetch_passes(frame.bbox()), []), ground)
    found = []
    if trip.show.sights:
        with looking_up("your sights"):
            found = locate_sights(trip.stops)  # you asked for these: failing stops the run
    sights = [f.sight for f in found]
    if trip.show.auto_sights:
        mine = {f.sight.name.casefold() for f in found}
        candidates = optional("well-known sights", lambda: fetch_sight_candidates(frame.bbox()), [])
        sights += [x for x in auto_sights(candidates, ground, stop_points) if x.name.casefold() not in mine]
    heights, highest = [None] * len(trip.stops), None
    if trip.show.altitudes:
        heights, top_marker, highest = optional(
            "altitudes", lambda: altitudes(stop_points, ground, passes), (heights, None, None))
        if top_marker:
            passes.append(top_marker)
    peak_list = optional("mountain peaks", lambda: fetch_peaks(frame.bbox()), [])
    for p in passes:
        print(f"     ▲ {p.name}" + (f" {units.height(p.ele, trip.units).lower()}" if p.ele else ""))
    for f in found:
        print(f"     ● {f.wanted} ({f.how})")

    text = PosterText(title=trip.title, subtitle=subtitle(trip),
                      stats=stats_line(trip, route, passes, highest) if trip.show.stats else "",
                      footer=footer_line(trip, frame))
    # A pass that is one of your stops (Khardung La) is counted above but drawn only as the stop.
    drawn_passes = [p for p in passes if not any(haversine_km((p.lon, p.lat), st) < STOP_PASS_KM for st in stop_points)]
    poster = Poster(frame=frame, features=features, roads=roads, sea=sea, route=route, stops=trip.stops,
                    stop_heights=heights, passes=drawn_passes, sights=sights, text=text, show=trip.show,
                    peaks=peak_list, border=border_text(trip, passes), units=trip.units, bleed=bleed_inches(trip))
    return poster, found


def map_layers(trip: Trip, frame: Frame) -> tuple[dict, list, list[str]]:
    """(features, sea, road classes to draw) for the poster area, downloading what isn't cached."""
    bbox, area = frame.bbox(), frame.area_km2()
    level = download_level(area, trip.map_detail)
    print(f"2/4  Map data ({area:,.0f} km², {level} detail)")
    # The sea first: squares that are all sea needn't be downloaded (an island trip's poster is mostly sea).
    sea = optional("the coastline", lambda: fetch_sea(bbox), [])
    done, total, at_sea = tiles_status(bbox, level, sea)
    skipped = f" ({at_sea} all-sea parts skipped)" if at_sea else ""
    if done == total:
        print(f"     using saved data{skipped}")
    else:
        print(f"     downloading {total - done} of {total} parts from OpenStreetMap{skipped}.\n"
              "     This can take a few minutes the first time; finished parts are saved.")

    progress = Progress(total - done) if done < total else None
    try:
        features = fetch_features(bbox, level, sea=sea, on_tile=progress and progress.part_done,
                                  notice=progress and progress.notice)
    finally:
        if progress:
            progress.close()
    scale = min(frame.width, frame.height) / 12
    roads = roads_to_draw(features, frame.km_per_inch(), frame.width * frame.height * MAP_SHARE, scale, trip.map_detail)
    print(f"     roads drawn: {', '.join(roads)}" + ("; sea" if sea else ""))
    return features, sea, roads


class Progress:
    """One live line while the map downloads, or (no `total`, a `label`) while one lookup runs:

        ████████░░░░░░░░░░░░░░░░ 8/31 parts · 4m12s · ~11m left · waiting for overpass-api.de
        mountain peaks · 45s · waiting for maps.mail.ru

    Redrawn every second, so a slow server doesn't look like a hang: the clock keeps running and
    the end says what the server is doing (mapdata.overpass's notices). When the output isn't a
    terminal (a log file), it prints plain "part i/n" lines and the server problems instead."""
    WIDTH = 24

    def __init__(self, total: int | None, label: str = ""):
        self.total, self.label, self.done, self.start, self.status = total, label, 0, time.time(), "starting"
        self.live = sys.stdout.isatty()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        if self.live:
            threading.Thread(target=self._tick, daemon=True).start()

    def part_done(self, i: int, n: int) -> None:
        with self.lock:
            self.done = i
        if self.live:
            self._draw()
        else:
            print(f"     part {i}/{n}", flush=True)

    def notice(self, text: str) -> None:
        with self.lock:
            changed, self.status = text != self.status, text
        if not self.live and changed and not text.startswith("waiting"):
            print(f"     {text}", flush=True)

    def close(self, final: str | None = None) -> None:
        """Stop the clock; `final` replaces the live line (and is printed when not live)."""
        self.stopped.set()
        if self.live:
            self._draw(final)
            print()
        elif final:
            print(f"     {final}", flush=True)

    def elapsed(self) -> float:
        return time.time() - self.start

    def _tick(self) -> None:
        while not self.stopped.wait(1):
            self._draw()

    def _draw(self, final: str | None = None) -> None:
        with self.lock:
            elapsed = self.elapsed()
            if final:
                line = f"     {final}"
            elif self.total is None:
                line = f"     {self.label} · {_clock(elapsed)} · {self.status}"
            else:
                filled = round(self.WIDTH * self.done / self.total)
                left = f" · ~{_clock(elapsed / self.done * (self.total - self.done))} left" if self.done else ""
                line = (f"     {'█' * filled}{'░' * (self.WIDTH - filled)} {self.done}/{self.total} parts"
                        f" · {_clock(elapsed)}{left} · {self.status}")
            width = shutil.get_terminal_size().columns - 1
            sys.stdout.write("\r" + line[:width].ljust(width))
            sys.stdout.flush()


def _clock(seconds: float) -> str:
    """75 -> '1m15s'."""
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


@contextmanager
def looking_up(what: str):
    """A live line while one lookup runs ("mountain peaks · 45s · waiting for maps.mail.ru"), then
    "✓ mountain peaks (52s)", or "✗ …" when it failed. Overpass's notices reach the line through
    net.NOTICE."""
    line = Progress(None, what)
    token = NOTICE.set(line.notice)
    ok = False
    try:
        yield
        ok = True
    finally:
        NOTICE.reset(token)
        line.close(f"{'✓' if ok else '✗'} {what} ({_clock(line.elapsed())})")


def optional(what: str, lookup, fallback):
    """Run a lookup for an optional detail (with its status line). If it fails (server busy,
    connection lost), say so and return `fallback`, so the poster is still made without that detail."""
    try:
        with looking_up(what):
            return lookup()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"     ⚠ Couldn't look up {what} ({first_line}); the poster is made without them.\n"
              "       Run it again later to add them (everything else is saved).")
        return fallback


def border_text(trip: Trip, passes: list) -> BorderText:
    """What the runes in a Pelennor Fields frame say. Above: the stops in order (a place you return to once).
    Below: the days, the passes and the highest pass crossed, in words, because runes have no
    digits. The sides: the trip file's runes_left / runes_right, else the theme's."""
    names: list[str] = []
    for stop in trip.stops:
        if not stop.via and stop.name not in names:
            names.append(stop.name)
    facts = []
    days = trip_days(trip)
    if days:
        facts.append(f"{number_words(days)} day{'s' if days != 1 else ''}")
    named = [p for p in passes if p.name != "Highest point"]
    if named:
        facts.append(f"{number_words(len(named))} pass{'es' if len(named) != 1 else ''}")
        top = max(named, key=lambda p: p.ele or 0)
        facts.append(f"over {top.name}")
    return BorderText(top=" · ".join(names), bottom=" · ".join(facts) or trip.title,
                      left=trip.runes_left, right=trip.runes_right)


def subtitle(trip: Trip) -> str:
    """The subtitle line. "auto": the places you stayed at (or, without nights, all stops), up to 5;
    more than that becomes "Start → End" or "Start loop"."""
    if trip.subtitle != "auto":
        return trip.subtitle or ""
    stops = [s for s in trip.stops if not s.via]
    stayed = [s for s in stops if s.nights] or stops
    names: list[str] = []
    for s in stayed:
        if s.name not in names:
            names.append(s.name)
    if len(names) <= 5:
        return " · ".join(names)
    if is_loop(stops):
        return f"{stops[0].name} loop"
    return f"{stops[0].name} → {stops[-1].name}"


def trip_days(trip: Trip) -> int | None:
    """`days` from the trip file, else total nights + 1, else unknown (None)."""
    if trip.days:
        return trip.days
    nights = sum(s.nights for s in trip.stops)
    return nights + 1 if nights else None


def stats_line(trip: Trip, route: Route, passes: list, highest: float | None) -> str:
    """'1,188 KM  ·  6 DAYS  ·  2 PASSES  ·  HIGHEST 4,551 M' (imperial: '738 MI … HIGHEST 14,931 FT');
    anything unknown is left out."""
    parts = [units.distance(route.on_map_km, trip.units)]
    days = trip_days(trip)
    if days:
        parts.append(f"{days} DAY{'S' if days != 1 else ''}")
    n_passes = sum(1 for p in passes if p.name != "Highest point")
    if n_passes:
        parts.append(f"{n_passes} PASS{'ES' if n_passes != 1 else ''}")
    if highest:
        parts.append(f"HIGHEST {units.height(highest, trip.units)}")
    return SEP.join(parts)


def footer_line(trip: Trip, frame: Frame) -> str:
    """'27 DEC 2026 – 1 JAN 2027  ·  10.05° N / 76.89° E' (the dates part only when set)."""
    s, w, n, e = frame.bbox(0)
    lat, lon = (s + n) / 2, (w + e) / 2
    coords = f"{abs(lat):.2f}° {'N' if lat >= 0 else 'S'} / {abs(lon):.2f}° {'E' if lon >= 0 else 'W'}"
    return SEP.join(p for p in ((trip.dates or "").upper(), coords) if p)
