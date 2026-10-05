"""The trip file: what's in it (dataclasses), writing it (`write_trip`) and reading it back (`load_trip`).

The trip file is the single place where the user configures a poster. `routetoposter new` writes it
from a Google Maps link; the user edits it; `preview` / `make` read it.
"""
import difflib
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .errors import UserError

SIZES = {  # name -> (width, height, unit), portrait
    "8x10": (8, 10, "in"), "12x16": (12, 16, "in"), "18x24": (18, 24, "in"), "24x36": (24, 36, "in"),
    "A4": (210, 297, "mm"), "A3": (297, 420, "mm"), "A2": (420, 594, "mm"),
    "instagram": (1080, 1350, "px"), "phone": (1179, 2556, "px"), "wallpaper": (2160, 3840, "px"),
}
ORIENTATIONS = ("portrait", "landscape")
FORMATS = ("png", "pdf", "svg")
MAP_DETAILS = ("auto", "more", "less")
ROUTE_MODES = ("driving", "straight")
# Kinds a sight can be; each has its own map symbol (render.SIGHT_SYMBOL).
SIGHT_KINDS = ("monastery", "temple", "church", "mosque", "fort", "palace", "viewpoint", "waterfall",
               "lake", "beach", "lighthouse", "peak", "park", "museum", "hot_spring", "dam", "sight")


@dataclass
class NearbySight:
    """A place the user saw near a stop. Only `name` is required: without lat/lon it's looked up in
    OpenStreetMap near its stop (sights.py); without `kind` the kind comes from OpenStreetMap."""
    name: str
    kind: str | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass
class Stop:
    name: str  # the label on the poster
    lat: float
    lon: float
    nights: int = 0  # nights slept here; 0 = passed through or visited
    label: bool = True  # False hides the name (the marker stays)
    via: bool = False  # True = only shapes the route: no marker, no label
    sights_nearby: list[NearbySight] = field(default_factory=list)
    link_name: str | None = None  # Google's full name for the place; only written as a comment


@dataclass
class Show:
    """Switches for what gets drawn."""
    stop_labels: bool = True
    altitudes: bool = True
    passes: bool = True
    sights: bool = True
    auto_sights: bool = False
    water_names: bool = True
    scale_bar: bool = True
    arrows: bool = True
    stats: bool = True


@dataclass
class Trip:
    stops: list[Stop]
    link: str | None = None
    title: str = "Road Trip"
    subtitle: str | None = "auto"  # "auto" = made from the stops; None/"" = no subtitle
    dates: str | None = None
    days: int | None = None  # overrides "total nights + 1"
    theme: str = "terracotta"
    size: str = "12x16"
    orientation: str = "portrait"
    dpi: int = 300
    format: str = "png"
    map_detail: str = "auto"
    route: str = "driving"
    runes_left: str | None = None  # pelennor_fields theme: the runes down the left edge (None = the theme's)
    runes_right: str | None = None  # ... and down the right edge
    show: Show = field(default_factory=Show)


# ── writing ──────────────────────────────────────────────────────────────────────────────────────

def write_trip(trip: Trip, path: Path, route_lines: list[str], themes: list[str]) -> str:
    """Write `trip` as a commented YAML file and return the text. `route_lines` are printed as a
    read-only comment block (the distance of each leg); `themes` are listed next to `theme:`."""
    v = _scalar
    lines = [
        "# RouteToPoster trip file. Edit anything below, then run:",
        f"#   routetoposter preview {path}     (quick low-resolution check)",
        f"#   routetoposter make {path}        (the final poster)",
        "",
        "# ── Text on the poster ───────────────────────────────────────────────",
        f"title: {v(trip.title)}",
        f"subtitle: {v(trip.subtitle)}   # auto = the places you stayed at; or your own text; \"\" = none",
        f"dates: {v(trip.dates) if trip.dates else ''}   # free text, e.g. 27 Dec 2026 – 1 Jan 2027; \"\" = not shown",
        "# days: 6                 # optional; normally total nights + 1",
        "",
        "# ── Look ─────────────────────────────────────────────────────────────",
        f"theme: {v(trip.theme)}   # {', '.join(themes)}",
        f"size: {v(trip.size)}   # {' | '.join(SIZES)}",
        f"orientation: {trip.orientation}   # portrait | landscape",
        f"dpi: {trip.dpi}   # 150 = screen, 300 = print, 600 = big print (ignored for pixel sizes)",
        f"format: {trip.format}   # png | pdf | svg",
        f"map_detail: {trip.map_detail}   # auto | more | less  (how many small roads are drawn)",
        f"route: {trip.route}   # driving = follow the roads; straight = straight lines between stops",
        "# runes_left: The road goes ever on and on   # pelennor_fields theme: runes down the left edge",
        "# runes_right: There and back again          # pelennor_fields theme: runes down the right edge",
        "",
        "# ── Stops ────────────────────────────────────────────────────────────",
        "# nights: nights you slept there (0 = passed through or visited).",
        "# sights_nearby: places you saw around this stop. Each one is either",
        "#     - Key Monastery                               (found in OpenStreetMap near the stop)",
        "#     - {name: Key Monastery, kind: monastery}      (also choose the symbol)",
        "#     - {name: Key Monastery, lat: 32.29, lon: 78.01}   (exact spot, no lookup)",
        f"#   kinds: {', '.join(SIGHT_KINDS)}",
        "# label: false hides the name; via: true only shapes the route (no marker, no name).",
        "# lat / lon come from the Google Maps link; leave them as they are.",
        "stops:",
    ]
    for stop in trip.stops:
        lines.append(f"  - name: {v(stop.name)}" + (f"   # {stop.link_name}" if stop.link_name else ""))
        lines.append(f"    nights: {stop.nights}")
        lines.append("    sights_nearby: []" if not stop.sights_nearby else "    sights_nearby:")
        lines.extend(f"      - {_sight_yaml(s)}" for s in stop.sights_nearby)
        if not stop.label:
            lines.append("    label: false")
        if stop.via:
            lines.append("    via: true")
        lines.append(f"    lat: {stop.lat}")
        lines.append(f"    lon: {stop.lon}")
    lines += ["", "# ── Route (worked out from the link; for your information only) ──────"]
    lines += [f"#   {line}" for line in route_lines]
    lines += ["", "# ── What to draw ────────────────────────────────────────────────────", "show:"]
    comments = {
        "stop_labels": "names next to the stops (with altitude and nights)",
        "altitudes": "stop altitudes and the highest point",
        "passes": "▲ mountain passes on the route",
        "sights": "your sights_nearby",
        "auto_sights": "also add well-known sights found along the route",
        "water_names": "river and lake names",
        "scale_bar": "scale bar and north arrow",
        "arrows": "direction arrows inside the route line",
        "stats": "the distance · days · passes · highest point line",
    }
    for f in fields(Show):
        lines.append(f"  {f.name}: {str(getattr(trip.show, f.name)).lower()}   # {comments[f.name]}")
    lines += ["", "# The Google Maps link these stops came from (kept for reference; not read again):",
              f"link: {v(trip.link)}"]
    text = "\n".join(_align_comment(line) for line in lines) + "\n"
    path.write_text(text)
    return text


COMMENT_COLUMN = 26


def _align_comment(line: str) -> str:
    """Line up the '# …' after a 'key: value' at COMMENT_COLUMN (comment-only lines stay as they are)."""
    if line.lstrip().startswith("#") or "   # " not in line:
        return line
    setting, comment = line.split("   # ", 1)
    return f"{setting:<{COMMENT_COLUMN}} # {comment}"


def _scalar(value) -> str:
    """One value as YAML text, quoted only when YAML needs it ('Kochi' stays bare, 'A: B' is quoted)."""
    return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000).strip().removesuffix("\n...").strip()


def _sight_yaml(sight: NearbySight) -> str:
    if sight.kind is None and sight.lat is None:
        return _scalar(sight.name)
    parts = [f"name: {_scalar(sight.name)}"]
    if sight.kind:
        parts.append(f"kind: {sight.kind}")
    if sight.lat is not None:
        parts += [f"lat: {sight.lat}", f"lon: {sight.lon}"]
    return "{" + ", ".join(parts) + "}"


# ── reading ──────────────────────────────────────────────────────────────────────────────────────

def load_trip(path: Path, themes: list[str]) -> Trip:
    """Read and check a trip file. Every problem is reported as a UserError naming the setting (and,
    for a typo, the closest valid name); nothing unknown is silently ignored. `themes` are the valid
    theme names."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise UserError(f"{path} isn't valid YAML (often a missing space after ':' or a bad indent):\n{e}")
    if not isinstance(raw, dict):
        raise UserError(f"{path} should be a list of settings like 'title: Kerala'.")

    top = {f.name for f in fields(Trip)}
    _no_unknown_keys(raw, top, "setting")
    if not isinstance(raw.get("stops"), list) or len(raw["stops"]) < 2:
        raise UserError("'stops' needs at least two stops.")

    trip = Trip(stops=[_read_stop(item, i) for i, item in enumerate(raw["stops"], 1)])
    trip.link = _opt_text(raw, "link")
    trip.title = _text(raw, "title", trip.title)
    trip.subtitle = _opt_text(raw, "subtitle") if "subtitle" in raw else trip.subtitle
    trip.dates = _opt_text(raw, "dates")
    trip.days = _opt_int(raw, "days", minimum=1)
    trip.theme = _choice(raw, "theme", themes, trip.theme)
    trip.size = _choice(raw, "size", list(SIZES), trip.size)
    trip.orientation = _choice(raw, "orientation", ORIENTATIONS, trip.orientation)
    trip.dpi = _opt_int(raw, "dpi", minimum=50, maximum=1200) or trip.dpi
    trip.format = _choice(raw, "format", FORMATS, trip.format)
    trip.map_detail = _choice(raw, "map_detail", MAP_DETAILS, trip.map_detail)
    trip.route = _choice(raw, "route", ROUTE_MODES, trip.route)
    trip.runes_left = _opt_text(raw, "runes_left")
    trip.runes_right = _opt_text(raw, "runes_right")
    trip.show = _read_show(raw.get("show") or {})
    if sum(not s.via for s in trip.stops) < 2:
        raise UserError("At least two stops must not be 'via: true'.")
    return trip


def _read_stop(item, number: int) -> Stop:
    where = f"stop {number}"
    if not isinstance(item, dict):
        raise UserError(f"{where} should look like '- name: Kochi' with nights, lat and lon below it.")
    _no_unknown_keys(item, {f.name for f in fields(Stop)} - {"link_name"}, f"key in {where}")
    name = _text(item, "name", None, where)
    if name is None:
        raise UserError(f"{where} has no name.")
    where = f"stop {number} ({name})"
    lat, lon = item.get("lat"), item.get("lon")
    if not (_is_number(lat) and _is_number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise UserError(f"{where} needs lat and lon as numbers (they come from the Google Maps link).")
    sights = item.get("sights_nearby") or []
    if not isinstance(sights, list):
        raise UserError(f"sights_nearby in {where} should be a list, one '- name' per line.")
    return Stop(name=name, lat=float(lat), lon=float(lon),
                nights=_opt_int(item, "nights", minimum=0, where=where) or 0,
                label=_bool(item, "label", True, where), via=_bool(item, "via", False, where),
                sights_nearby=[_read_sight(s, where) for s in sights])


def _read_sight(item, where: str) -> NearbySight:
    if isinstance(item, str) and item.strip():
        return NearbySight(name=item.strip())
    if not isinstance(item, dict):
        raise UserError(f"A sight in {where} should be a name, or {{name: …, kind: …, lat: …, lon: …}}.")
    _no_unknown_keys(item, {f.name for f in fields(NearbySight)}, f"key for a sight in {where}")
    name = _text(item, "name", None, where)
    if not name:
        raise UserError(f"A sight in {where} has no name.")
    kind = _choice(item, "kind", SIGHT_KINDS, None, f"sight {name!r}")
    lat, lon = item.get("lat"), item.get("lon")
    if (lat is None) != (lon is None) or (lat is not None and not (_is_number(lat) and _is_number(lon))):
        raise UserError(f"Sight {name!r} in {where} needs both lat and lon as numbers, or neither.")
    return NearbySight(name=name, kind=kind, lat=lat, lon=lon)


def _read_show(raw) -> Show:
    if not isinstance(raw, dict):
        raise UserError("'show' should be a list of switches like 'passes: true'.")
    _no_unknown_keys(raw, {f.name for f in fields(Show)}, "switch under 'show'")
    show = Show()
    for f in fields(Show):
        setattr(show, f.name, _bool(raw, f.name, getattr(show, f.name), "show"))
    return show


# Small typed getters. Each raises a UserError that names the setting and says what it should be.

def _no_unknown_keys(raw: dict, known: set[str], what: str) -> None:
    for key in raw:
        if key not in known:
            close = difflib.get_close_matches(str(key), sorted(known), n=1)
            hint = f' Did you mean "{close[0]}"?' if close else f" Valid: {', '.join(sorted(known))}."
            raise UserError(f'"{key}" isn\'t a {what}.{hint}')


def _text(raw: dict, key: str, default, where: str = "the trip file"):
    value = raw.get(key, default)
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        raise UserError(f"'{key}' in {where} should be text.")
    return str(value).strip()


def _opt_text(raw: dict, key: str) -> str | None:
    value = _text(raw, key, None)
    return value or None


def _opt_int(raw: dict, key: str, minimum: int | None = None, maximum: int | None = None,
             where: str = "the trip file") -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UserError(f"'{key}' in {where} should be a whole number, not {value!r}.")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise UserError(f"'{key}' in {where} should be between {minimum} and {maximum or 'any'}, not {value}.")
    return value


def _bool(raw: dict, key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise UserError(f"'{key}' in {where} should be true or false, not {value!r}.")
    return value


def _choice(raw: dict, key: str, valid, default, where: str = "the trip file"):
    value = raw.get(key)
    if value is None:
        return default
    by_lower = {str(v).lower(): v for v in valid}
    if str(value).lower() in by_lower:
        return by_lower[str(value).lower()]
    close = difflib.get_close_matches(str(value), list(map(str, valid)), n=1)
    hint = f' Did you mean "{close[0]}"?' if close else ""
    raise UserError(f"'{key}: {value}' in {where} isn't valid.{hint}\n  Choose one of: {', '.join(map(str, valid))}")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
