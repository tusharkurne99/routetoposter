"""Draw the poster with matplotlib. No network access here: everything to draw arrives in a Poster.

Layers, bottom to top (the `zorder` values below):
    0.8 sea · 1 glaciers, lakes · 2.x rivers, streams, roads · 5–6.5 the route (casing, line,
    arrows) · 7.x markers and symbols · 9 fades and the solid title band · 9.5 water names ·
    10 labels · 11 title block, scale bar
Sizes are in points on a 12x16 in poster and multiplied by `s` (short edge / 12 in), so every
poster size looks the same.
"""
from dataclasses import dataclass, field

import matplotlib

matplotlib.use("Agg")  # draw to files only; no window
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import colors as mcolors  # noqa: E402
from matplotlib import patheffects  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402

from .extras import Pass, Sight  # noqa: E402
from .layout import KM_PER_DEG, ROUTE_BOX, Frame  # noqa: E402
from .mapdata import LINE_WIDTH_PT, is_latin  # noqa: E402
from .route import Route, haversine_km  # noqa: E402
from .trip import Show, Stop  # noqa: E402

ROUTE_WIDTH, CASING_WIDTH = 4.0, 9.0
LOOP_KM = 1.0  # start and end closer than this: the trip is a loop
SEP = "  ·  "
SYMBOL_FONT = "DejaVu Sans"  # ships with matplotlib and has every symbol below
SIGHT_SYMBOL = {"monastery": "☸", "temple": "✸", "church": "✝", "mosque": "☪", "fort": "♜", "palace": "♛",
                "viewpoint": "✦", "waterfall": "≋", "lake": "≈", "beach": "☼", "lighthouse": "◉",
                "peak": "△", "park": "♣", "museum": "⌂", "hot_spring": "♨", "dam": "≡", "sight": "●"}
CONNECTOR_MIN_IN = 0.35  # a sight further than this (× s) from its stop gets a dotted line to it
NICE_KM = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]


@dataclass
class PosterText:
    title: str
    subtitle: str  # "" = none
    stats: str  # "833 KM  ·  9 DAYS  ·  3 PASSES  ·  HIGHEST 4,551 M"; "" = none
    footer: str  # "JUNE 2026  ·  31.65° N / 77.88° E"


@dataclass
class Poster:
    """Everything one poster needs, already fetched and chosen."""
    frame: Frame
    features: dict  # mapdata.fetch_features
    roads: list[str]  # road classes to draw (mapdata.roads_to_draw)
    sea: list  # mapdata.fetch_sea
    route: Route
    stops: list[Stop]
    stop_heights: list[float | None]  # one per stop; None when altitudes are off
    passes: list[Pass]
    sights: list[Sight]
    text: PosterText
    show: Show = field(default_factory=Show)


@dataclass
class Label:
    """A label waiting to be placed next to its marker at (x, y)."""
    x: float
    y: float
    title: str
    sub: str
    tier: str  # "major" (bold, big) | "minor" | "small"
    r: float  # marker radius (inches), so the label clears it
    optional: bool = False  # skipped when there's no free spot (instead of overlapping)


def render(path: str, poster: Poster, theme: dict, fonts: dict, dpi: float) -> None:
    """Draw `poster` in `theme` and save it to `path` (png, pdf or svg by its extension)."""
    frame, show = poster.frame, poster.show
    W, H = frame.width, frame.height
    s = min(W, H) / 12
    bg = theme["bg"]

    fig = plt.figure(figsize=(W, H), facecolor=bg)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)

    def proj(points) -> np.ndarray:
        a = np.asarray(points, dtype=float)
        x, y = frame.project(a[:, 0], a[:, 1])
        return np.column_stack((x, y))

    _draw_map(ax, poster, theme, s, proj)

    rx, ry = frame.project([c[0] for c in poster.route.coords], [c[1] for c in poster.route.coords])
    route_xy = np.column_stack((rx, ry))
    round_line = {"solid_capstyle": "round", "solid_joinstyle": "round"}
    ax.plot(rx, ry, color=bg, lw=CASING_WIDTH * s, alpha=0.85, zorder=5, **round_line)
    ax.plot(rx, ry, color=theme["route"], lw=ROUTE_WIDTH * s, zorder=6, **round_line)

    shown = [(st, h) for st, h in zip(poster.stops, poster.stop_heights) if not st.via]
    stop_xy = [tuple(float(v) for v in frame.project(st.lon, st.lat)) for st, _ in shown]
    if show.arrows:
        _arrows(ax, route_xy, stop_xy, bg, s)

    layout, text_top = _text_layout(poster.text, s)
    solid_top = text_top + 0.25 * s

    labels = (_stop_markers(ax, frame, shown, theme, s, show.stop_labels)
              + _pass_markers(ax, frame, poster.passes, theme, s)
              + _sight_markers(ax, frame, poster.sights, theme, s))
    labels.sort(key=lambda lb: ({"major": 0, "minor": 1, "small": 2}[lb.tier], lb.optional))
    placed = _place_labels(ax, labels, theme, fonts, s)
    map_area = (0.3 * s, solid_top + 0.35 * s, W - 0.3 * s, 0.86 * H)
    if show.water_names:
        _water_labels(ax, frame, poster.features, route_xy[:: max(1, len(rx) // 3000)], placed, theme, fonts, s, map_area)

    fade_top = max(ROUTE_BOX[1] * H, solid_top + 0.6 * s)
    _band(ax, theme["gradient_color"], W, 0, solid_top)
    _fade(ax, theme["gradient_color"], W, solid_top, fade_top, solid_low=True)
    _fade(ax, theme["gradient_color"], W, 0.88 * H, H, solid_low=False)

    _draw_text(fig, ax, poster.text, layout, theme, fonts, s, W, H)
    if show.scale_bar:
        _scale_bar(ax, frame, theme, fonts, s)
    fig.savefig(path, dpi=dpi, facecolor=bg)
    plt.close(fig)


# ── the background map ───────────────────────────────────────────────────────────────────────────

def _draw_map(ax, poster: Poster, theme: dict, s: float, proj) -> None:
    """Sea, glaciers, lakes, then rivers and the chosen road classes (minor roads first, so major
    roads lie on top)."""
    if poster.sea:
        vertices, codes = [], []
        for polygon in poster.sea:
            for ring in polygon:  # outline, then islands; even-odd filling leaves the islands empty
                xy = proj(ring)
                vertices.extend(xy)
                codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(xy) - 2) + [MplPath.CLOSEPOLY])
        ax.add_patch(PathPatch(MplPath(vertices, codes), facecolor=theme["water"], edgecolor="none", zorder=0.8))

    for key, color_key in (("glacier", "parks"), ("lake", "water")):
        polygons = [proj(p) for p in poster.features.get(key, [])]
        if polygons:
            ax.add_collection(PolyCollection(polygons, facecolors=theme[color_key], edgecolors="none", zorder=1))

    order = ["stream", "river"] + [c for c in ["default", "residential", "tertiary", "secondary", "primary", "motorway"]
                                   if c in poster.roads]
    for i, key in enumerate(order):
        lines = [proj(line) for line in poster.features.get(key, [])]
        if not lines:
            continue
        color = theme["water_line"] if key in ("stream", "river") else theme[f"road_{key}"]
        ax.add_collection(LineCollection(lines, colors=color, linewidths=LINE_WIDTH_PT[key] * s,
                                         capstyle="round", joinstyle="round", zorder=2 + i * 0.1))


# ── markers ──────────────────────────────────────────────────────────────────────────────────────

def is_loop(stops: list[Stop]) -> bool:
    return len(stops) > 2 and haversine_km((stops[0].lon, stops[0].lat), (stops[-1].lon, stops[-1].lat)) < LOOP_KM


def _altitude(m: float | None) -> str:
    return f"{m:,.0f} M" if m is not None else ""


def _nights(n: int) -> str:
    return f"{n} NIGHT{'S' if n != 1 else ''}" if n else ""


def _join(*parts: str) -> str:
    return SEP.join(p for p in parts if p)


def _radius(diameter_pt: float, s: float) -> float:
    """Marker radius in inches from its diameter in points."""
    return diameter_pt * s / 144


def _stop_markers(ax, frame: Frame, stops: list[tuple[Stop, float | None]], theme: dict, s: float,
                  with_labels: bool) -> list[Label]:
    """Draw the stop markers and return their labels (none when `with_labels` is False; a stop
    with `label: false` never gets one).

    Start/finish: a big dot (a loop gets one dot with a ring around it). Overnight stops: a disc
    with a hollow centre. Other stops: a small dot. If no stop has nights set, overnight and visit
    can't be told apart, so every middle stop gets the same ring. A place visited twice gets one
    marker and one label."""
    bg, route = theme["bg"], theme["route"]
    labels: list[Label] = []
    plain = [st for st, _ in stops]
    loop = is_loop(plain)
    any_nights = any(st.nights for st in plain)
    seen: list[Stop] = []
    for i, (st, height) in enumerate(stops):
        if any(haversine_km((st.lon, st.lat), (o.lon, o.lat)) < LOOP_KM for o in seen):
            continue
        seen.append(st)
        x, y = (float(v) for v in frame.project(st.lon, st.lat))
        first, last = i == 0, i == len(stops) - 1
        nights = sum(o.nights for o in plain if haversine_km((st.lon, st.lat), (o.lon, o.lat)) < LOOP_KM)
        if first or last:
            ax.scatter([x], [y], s=(13 * s) ** 2, facecolors=route, edgecolors=bg, linewidths=2 * s, zorder=8)
            if loop:
                ax.scatter([x], [y], s=(22 * s) ** 2, facecolors="none", edgecolors=route, linewidths=2.2 * s, zorder=8)
                role = "START · FINISH"
            elif last:
                ax.scatter([x], [y], s=(6 * s) ** 2, facecolors=bg, edgecolors="none", zorder=8.1)
                role = "FINISH"
            else:
                role = "START"
            sub, tier, r = _join(role, _altitude(height), _nights(nights)), "major", _radius(22 if loop else 13, s)
        elif not any_nights:
            ax.scatter([x], [y], s=(8 * s) ** 2, facecolors=bg, edgecolors=route, linewidths=2.4 * s, zorder=7)
            sub, tier, r = _altitude(height), "major", _radius(8, s)
        elif nights:
            ax.scatter([x], [y], s=(12 * s) ** 2, facecolors=route, edgecolors=bg, linewidths=1.5 * s, zorder=7)
            ax.scatter([x], [y], s=(4.5 * s) ** 2, facecolors=bg, edgecolors="none", zorder=7.1)
            sub, tier, r = _join(_altitude(height), _nights(nights)), "major", _radius(12, s)
        else:
            ax.scatter([x], [y], s=(6 * s) ** 2, facecolors=route, edgecolors=bg, linewidths=1.2 * s, zorder=7)
            sub, tier, r = _altitude(height), "minor", _radius(6, s)
        if with_labels and st.label:
            labels.append(Label(x, y, st.name.upper(), sub, tier, r))
    return labels


def _pass_markers(ax, frame: Frame, passes: list[Pass], theme: dict, s: float) -> list[Label]:
    """▲ for each pass (and the "Highest point" marker), with its altitude."""
    labels = []
    for p in passes:
        x, y = (float(v) for v in frame.project(p.lon, p.lat))
        ax.scatter([x], [y], marker="^", s=(9 * s) ** 2, facecolors=theme["text"], edgecolors=theme["bg"],
                   linewidths=1.2 * s, zorder=7.5)
        labels.append(Label(x, y, p.name.upper(), _altitude(p.ele), "minor", _radius(9, s)))
    return labels


def _sight_markers(ax, frame: Frame, sights: list[Sight], theme: dict, s: float) -> list[Label]:
    """A symbol per sight (SIGHT_SYMBOL). A user's sight far from its stop gets a dotted line to the
    stop, so it's clear which stop it belongs to. User sights always get a label; auto sights only
    where there's room."""
    fp = FontProperties(family=SYMBOL_FONT, size=9 * s)
    halo = [patheffects.withStroke(linewidth=2.2 * s, foreground=theme["bg"])]
    labels = []
    for sight in sights:
        x, y = (float(v) for v in frame.project(sight.lon, sight.lat))
        if sight.stop:
            sx, sy = (float(v) for v in frame.project(*sight.stop))
            if np.hypot(x - sx, y - sy) > CONNECTOR_MIN_IN * s:
                ax.plot([sx, x], [sy, y], color=theme["text"], lw=0.6 * s, alpha=0.6,
                        linestyle=(0, (1, 2)), zorder=6.8)
        ax.text(x, y, SIGHT_SYMBOL.get(sight.kind, "●"), ha="center", va="center", color=theme["text"],
                fontproperties=fp, path_effects=halo, zorder=7.6)
        title = sight.name if sight.stop else sight.name.upper()  # the user's sights in their own spelling
        labels.append(Label(x, y, title, "", "small", _radius(9, s), optional=sight.stop is None))
    return labels


# ── labels ───────────────────────────────────────────────────────────────────────────────────────

TIER_SIZES = {"major": (10, 7.2), "minor": (8.5, 6.6), "small": (7, 6)}  # (name, detail) points


def _place_labels(ax, labels: list[Label], theme: dict, fonts: dict, s: float) -> list[tuple]:
    """Place each label (most important first) right, left, above or below its marker: the first
    spot that overlaps no earlier label or marker. If none is free, a required label goes to the
    right anyway; an optional one is skipped. Returns every occupied box, so water names can avoid
    them."""
    halo = [patheffects.withStroke(linewidth=3 * s, foreground=theme["bg"])]
    placed = [(lb.x - lb.r, lb.y - lb.r, lb.x + lb.r, lb.y + lb.r) for lb in labels]

    for lb in labels:
        if not is_latin(lb.title):
            continue  # the fonts can't draw it
        t_size, d_size = (v * s for v in TIER_SIZES[lb.tier])
        gap = lb.r + 0.07 * s
        h = t_size / 72 + ((0.03 * s + d_size / 72) if lb.sub else 0)
        w = max(len(lb.title) * t_size * 0.70, len(lb.sub) * d_size * 0.64) / 72
        x, y = lb.x, lb.y
        options = [  # (box, horizontal alignment)
            ((x + gap, y - h / 2, x + gap + w, y + h / 2), "left"),
            ((x - gap - w, y - h / 2, x - gap, y + h / 2), "right"),
            ((x - w / 2, y + gap, x + w / 2, y + gap + h), "center"),
            ((x - w / 2, y - gap - h, x + w / 2, y - gap), "center"),
        ]
        own = (x - lb.r, y - lb.r, x + lb.r, y + lb.r)
        others = [p for p in placed if p != own]
        free = next((o for o in options if not any(_overlap(o[0], p) for p in others)), None)
        if free is None and lb.optional:
            continue
        box, ha = free or options[0]
        placed.append(box)

        tx = {"left": box[0], "right": box[2], "center": (box[0] + box[2]) / 2}[ha]
        title_fp = (fonts["bold"] if lb.tier == "major" else fonts["regular"]).copy()
        title_fp.set_size(t_size)
        ax.text(tx, box[3], lb.title, ha=ha, va="top", color=theme["text"], fontproperties=title_fp,
                path_effects=halo, zorder=10)
        if lb.sub:
            sub_fp = fonts["light"].copy()
            sub_fp.set_size(d_size)
            ax.text(tx, box[1], lb.sub, ha=ha, va="bottom", color=theme["text"], alpha=0.85,
                    fontproperties=sub_fp, path_effects=halo, zorder=10)
    return placed


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ── direction arrows ─────────────────────────────────────────────────────────────────────────────

def _arc(xy: np.ndarray) -> np.ndarray:
    """Distance along the line at each point."""
    return np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))))


def _at(xy: np.ndarray, arc: np.ndarray, t) -> np.ndarray:
    """The points at distances `t` along the line."""
    return np.column_stack((np.interp(t, arc, xy[:, 0]), np.interp(t, arc, xy[:, 1])))


def _arrows(ax, xy: np.ndarray, stop_xy: list, bg: str, s: float) -> None:
    """Faint chevrons inside the route line, pointing the way you travelled. Skipped near stop
    markers and where the route runs over itself (an out-and-back road would get arrows both ways)."""
    arc = _arc(xy)
    spacing, half, look = 0.9 * s, 0.02 * s, 0.05 * s
    stops = np.array(stop_xy) if stop_xy else np.empty((0, 2))
    chevrons = []
    for t in np.arange(spacing / 2, arc[-1] - look, spacing):
        p = _at(xy, arc, [t])[0]
        if len(stops) and np.hypot(*(stops - p).T).min() < 0.2 * s:
            continue
        far = np.abs(arc - t) > 0.5 * s
        if far.any() and np.hypot(*(xy[far] - p).T).min() < 0.06 * s:
            continue
        a, b = _at(xy, arc, [t - look, t + look])
        d = b - a
        n = np.hypot(*d)
        if n == 0:
            continue
        d /= n
        perp = np.array([-d[1], d[0]])
        chevrons.append([p - d * half + perp * half * 1.2, p + d * half, p - d * half - perp * half * 1.2])
    if chevrons:
        ax.add_collection(LineCollection(chevrons, colors=bg, linewidths=1.1 * s, alpha=0.85,
                                         capstyle="round", joinstyle="round", zorder=6.5))


# ── river and lake names ─────────────────────────────────────────────────────────────────────────

RIVER_SUFFIXES = (" river", " nadi", " nala", " nallah")


def _mix(a: str, b: str, t: float) -> tuple:
    """The colour `t` of the way from `a` to `b`."""
    ca, cb = np.array(mcolors.to_rgb(a)), np.array(mcolors.to_rgb(b))
    return tuple(ca * (1 - t) + cb * t)


def _rot_box(x: float, y: float, w: float, h: float, angle_deg: float) -> tuple:
    """Bounding box of a w×h box centred at (x, y) and rotated by `angle_deg`."""
    a = np.radians(angle_deg)
    hx = (abs(w * np.cos(a)) + abs(h * np.sin(a))) / 2
    hy = (abs(w * np.sin(a)) + abs(h * np.cos(a))) / 2
    return x - hx, y - hy, x + hx, y + hy


def river_name(name: str) -> str:
    """'Spiti River' -> 'Spiti': the line already says it's a river."""
    for suffix in RIVER_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def spaced_water_text(name: str) -> str:
    """Survey-map style letter spacing: 'Spiti' -> 'S P I T I'."""
    return "   ".join(" ".join(word) for word in name.upper().split())


def _water_labels(ax, frame: Frame, features: dict, route_xy: np.ndarray, placed: list, theme: dict,
                  fonts: dict, s: float, area: tuple, max_rivers: int = 6, max_lakes: int = 6) -> None:
    """Italic names along the rivers most visible on the map and beside named lakes. A river name
    follows a straight stretch of the river; anything that would collide with a label or the route
    is skipped."""
    color = _mix(theme["water_line"], theme["text"], 0.65)
    halo = [patheffects.withStroke(linewidth=2.2 * s, foreground=theme["bg"])]
    x0, y0, x1, y1 = area

    def proj(points) -> np.ndarray:
        a = np.asarray(points, dtype=float)
        x, y = frame.project(a[:, 0], a[:, 1])
        return np.column_stack((x, y))

    def clear(bx) -> bool:
        return bx[0] > x0 and bx[2] < x1 and bx[1] > y0 and bx[3] < y1 and not any(_overlap(bx, p) for p in placed)

    def near_route(points: np.ndarray, gap: float) -> bool:
        return any(np.hypot(*(route_xy - q).T).min() < gap for q in points)

    def visible_length(xy: np.ndarray) -> float:
        inside = (xy[:, 0] > x0) & (xy[:, 0] < x1) & (xy[:, 1] > y0) & (xy[:, 1] < y1)
        return float(np.hypot(*np.diff(xy, axis=0).T)[inside[1:] & inside[:-1]].sum())

    rivers: dict[str, list[np.ndarray]] = {}
    for name, pts in features.get("named_rivers", []):
        if is_latin(name):
            rivers.setdefault(river_name(name), []).append(proj(pts))
    ranked = sorted(rivers.items(), key=lambda kv: -sum(visible_length(a) for a in kv[1]))[:max_rivers]

    size = 8.5 * s
    fp = fonts["italic"].copy()
    fp.set_size(size)
    for name, parts in ranked:
        text = spaced_water_text(name)
        w, h = len(text) * size * 0.45 / 72, size / 72
        best = None  # (straightness, centre, angle, box)
        for xy in parts:
            arc = _arc(xy)
            if arc[-1] < w * 1.2:
                continue
            for start in np.arange(0, arc[-1] - w, w / 4):
                window = _at(xy, arc, np.linspace(start, start + w, 7))
                chord = np.hypot(*(window[-1] - window[0]))
                straight = chord / w
                if straight < 0.92 or (best and straight <= best[0]):
                    continue
                angle = np.degrees(np.arctan2(*(window[-1] - window[0])[::-1]))
                angle = angle - 180 if angle > 90 else angle + 180 if angle < -90 else angle
                direction = (window[-1] - window[0]) / chord
                normal = np.array([-direction[1], direction[0]])
                for shift in (0.0, 0.9 * h, -0.9 * h):  # on the river, else beside it
                    moved = window + normal * shift
                    mid = (moved[0] + moved[-1]) / 2
                    bx = _rot_box(mid[0], mid[1], w, h, angle)
                    if clear(bx) and not near_route(moved, 0.1 * s):
                        best = (straight, mid, angle, bx)
                        break
        if best:
            _, mid, angle, bx = best
            placed.append(bx)
            ax.text(mid[0], mid[1], text, rotation=angle, rotation_mode="anchor", ha="center", va="center",
                    color=color, fontproperties=fp, path_effects=halo, zorder=9.5)

    size = 7.5 * s
    fp = fonts["italic"].copy()
    fp.set_size(size)
    river_names = {n.lower() for n in rivers}

    def lake_rank(lake) -> tuple:
        x, y = (float(v) for v in frame.project(lake[1], lake[2]))
        near = np.hypot(*(route_xy - (x, y)).T).min() < 0.6 * s
        return (not near, -lake[3] * lake[4])  # lakes by the route first, then the biggest

    shown = 0
    for name, cx, cy, wdeg, hdeg in sorted(features.get("named_lakes", []), key=lake_rank):
        if river_name(name).lower() in river_names or not is_latin(name):
            continue  # a wide stretch of a river mapped as water
        x, y = (float(v) for v in frame.project(cx, cy))
        lw, lh = wdeg * frame.k * frame.c, hdeg * frame.k
        if max(lw, lh) < 0.04 * s or not (x0 < x < x1 and y0 < y < y1):
            continue
        text = name.upper()
        w, h = len(text) * size * 0.62 / 72, size / 72
        gx, gy = lw / 2 + 0.05 * s, lh / 2 + 0.05 * s
        options = [((x + gx, y - h / 2, x + gx + w, y + h / 2), "left"),
                   ((x - gx - w, y - h / 2, x - gx, y + h / 2), "right"),
                   ((x - w / 2, y + gy, x + w / 2, y + gy + h), "center"),
                   ((x - w / 2, y - gy - h, x + w / 2, y - gy), "center")]
        if max(lw, lh) > w * 1.2:  # a big lake: the name fits inside
            options.insert(0, ((x - w / 2, y - h / 2, x + w / 2, y + h / 2), "center"))
        spot = next((o for o in options if clear(o[0])), None)
        if not spot:
            continue
        bx, ha = spot
        placed.append(bx)
        tx = {"left": bx[0], "right": bx[2], "center": (bx[0] + bx[2]) / 2}[ha]
        ax.text(tx, (bx[1] + bx[3]) / 2, text, ha=ha, va="center", color=color, fontproperties=fp,
                path_effects=halo, zorder=9.5)
        shown += 1
        if shown == max_lakes:
            break


# ── scale bar, fades, title block ────────────────────────────────────────────────────────────────

def _scale_bar(ax, frame: Frame, theme: dict, fonts: dict, s: float) -> None:
    """A north arrow and a scale bar (a round number of km, at most 2 in long) in the bottom-left."""
    color = theme["text"]
    km_per_inch = KM_PER_DEG / frame.k
    km = max((n for n in NICE_KM if n / km_per_inch <= 2.0 * s), default=NICE_KM[0])
    length = km / km_per_inch
    x, y, tick = 0.75 * s, 0.32 * s, 0.05 * s

    ax.plot([x, x + length], [y, y], color=color, lw=0.9 * s, solid_capstyle="butt", zorder=11)
    for f in (0, 0.5, 1):
        ax.plot([x + f * length] * 2, [y, y + (tick if f != 0.5 else tick * 0.6)], color=color, lw=0.9 * s, zorder=11)
    fp = fonts["light"].copy()
    fp.set_size(6.5 * s)
    ax.text(x, y + tick * 1.6, "0", ha="center", va="bottom", color=color, alpha=0.8, fontproperties=fp, zorder=11)
    ax.text(x + length, y + tick * 1.6, f"{km} KM", ha="center", va="bottom", color=color, alpha=0.8,
            fontproperties=fp, zorder=11)

    nx, base, top, half = 0.35 * s, y - 0.02 * s, y + 0.2 * s, 0.045 * s
    ax.fill([nx - half, nx, nx], [base, top, base + 0.04 * s], color=color, zorder=11, lw=0)
    ax.fill([nx, nx + half, nx], [top, base, base + 0.04 * s], facecolor=theme["bg"], edgecolor=color,
            lw=0.6 * s, zorder=11)
    fp_n = fonts["regular"].copy()
    fp_n.set_size(7 * s)
    ax.text(nx, top + 0.03 * s, "N", ha="center", va="bottom", color=color, fontproperties=fp_n, zorder=11)


def _band(ax, color: str, W: float, y0: float, y1: float) -> None:
    """A solid band (behind the title block)."""
    ax.add_patch(plt.Rectangle((0, y0), W, y1 - y0, facecolor=color, edgecolor="none", zorder=9))


def _fade(ax, color: str, W: float, y0: float, y1: float, solid_low: bool) -> None:
    """A vertical fade from solid `color` to transparent, between heights y0 and y1."""
    img = np.empty((256, 1, 4))
    img[..., :3] = mcolors.to_rgb(color)
    img[:, 0, 3] = np.linspace(1, 0, 256) if solid_low else np.linspace(0, 1, 256)
    ax.imshow(img, extent=(0, W, y0, y1), origin="lower", aspect="auto", interpolation="bilinear", zorder=9)


def spaced_title(text: str) -> str:
    """maptoposter's letter-spaced title: 'Spiti' -> 'S  P  I  T  I'."""
    text = text.upper()
    return "  ".join(text) if text.isascii() else text


def _text_layout(text: PosterText, s: float) -> tuple[dict, float]:
    """Stack the title block up from the bottom margin: footer, stats, subtitle, rule, title. Empty
    lines take no room. Returns ({"y": baselines, "size": points}, top of the block in inches)."""
    sizes = {"footer": 10.5 * s, "stats": 12.5 * s, "subtitle": 18 * s,
             "title": 60 * s * min(1.0, 10 / max(len(text.title), 1))}
    y = {"footer": 0.6 * s}
    y["stats"] = y["footer"] + sizes["footer"] / 72 * 2.2
    y["subtitle"] = y["stats"] + (sizes["stats"] / 72 * 2.4 if text.stats else 0)
    y["rule"] = y["subtitle"] + (sizes["subtitle"] / 72 * 1.7 if text.subtitle else sizes["stats"] / 72 * 0.6)
    y["title"] = y["rule"] + 0.22 * s
    top = y["title"] + sizes["title"] / 72 * 0.75
    return {"y": y, "size": sizes}, top


def _draw_text(fig, ax, text: PosterText, layout: dict, theme: dict, fonts: dict, s: float, W: float, H: float) -> None:
    color = theme["text"]
    y, size = layout["y"], layout["size"]

    def font(role: str, pt: float):
        fp = fonts[role].copy()
        fp.set_size(pt)
        return fp

    fig.text(0.5, y["title"] / H, spaced_title(text.title), ha="center", color=color,
             fontproperties=font("bold", size["title"]), zorder=11)
    ax.plot([0.4 * W, 0.6 * W], [y["rule"]] * 2, color=color, lw=1 * s, zorder=11)
    if text.subtitle:
        fig.text(0.5, y["subtitle"] / H, text.subtitle.upper(), ha="center", color=color,
                 fontproperties=font("light", size["subtitle"]), zorder=11)
    if text.stats:
        fig.text(0.5, y["stats"] / H, text.stats, ha="center", color=color,
                 fontproperties=font("regular", size["stats"]), zorder=11)
    fig.text(0.5, y["footer"] / H, text.footer, ha="center", color=color, alpha=0.7,
             fontproperties=font("light", size["footer"]), zorder=11)
    fig.text(1 - 0.25 * s / W, 0.2 * s / H, "© OpenStreetMap contributors", ha="right", color=color,
             alpha=0.5, fontproperties=font("light", 6.5 * s), zorder=11)
