"""Drawing for the Pelennor Fields style (a theme with "style": "pelennor_fields"): an old map after
Thrór's Map in The Hobbit.

The map, route, markers and labels are drawn by render.py as for every theme. This module adds what
this style has instead of maptoposter's fades and spaced title:

- ink mountain symbols on the high ground (glaciers and named peaks), drawn under the route
- a double-ruled frame with runes in the border band, and a compass rose
- edges darkened like an old sheet of paper
- a title block: the title in runes, the title, subtitle, facts, and a footer with a scale bar
  in kilometres and leagues
- a small note that translates the runes

Sizes are in inches or points on a 12x16 in poster, multiplied by `s` like in render.py.
"""
import textwrap
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import colors as mcolors
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.textpath import TextPath

from .layout import KM_PER_DEG, NICE_KM, Frame
from .runes import number_words, to_runes

OUTER, INNER = 0.18, 0.46  # the two frame rules, inches in from the poster edge
LEAGUE_KM = 4.83  # three miles
MOUNTAIN_SPACING = 0.2  # inches between mountain symbols
MOUNTAIN_ROUTE_GAP = 0.22  # inches kept clear around the route
MAX_MOUNTAINS = 450
VIGNETTE_COLOR, VIGNETTE_ALPHA = "#462A0C", 0.38


@dataclass
class Layout:
    """Where the frame and title block go, in inches."""
    outer: float  # frame rules
    inner: float
    map_bottom: float  # the rule between the map and the title block
    y: dict  # baselines of the title block lines
    size: dict  # their sizes in points


@dataclass
class BorderText:
    """What the runes in the frame say, in English (render.py turns it into runes)."""
    top: str
    bottom: str
    left: str | None = None  # None = the theme's default
    right: str | None = None


def layout(text, s: float) -> Layout:
    """Stack the title block up from the inner frame rule: footer, facts, subtitle, title, the
    title in runes. Empty lines take no room. Long titles are drawn smaller."""
    inner = INNER * s
    n = max(len(text.title), 1)
    size = {"footer": 12 * s, "stats": 17 * s, "subtitle": 22 * s,
            "title": 55 * s * min(1.0, 14 / n), "title_runes": 26 * s * min(1.0, 16 / n)}
    y = {"footer": inner + 0.32 * s}
    y["stats"] = y["footer"] + 0.55 * s
    y["subtitle"] = y["stats"] + (size["stats"] / 72 * 2.0 if text.stats else 0)
    y["title"] = y["subtitle"] + (size["subtitle"] / 72 * 1.9 if text.subtitle else 0.1 * s)
    y["title_runes"] = y["title"] + size["title"] / 72 * 1.05
    map_bottom = y["title_runes"] + size["title_runes"] / 72 + 0.3 * s
    return Layout(OUTER * s, inner, map_bottom, y, size)


# ── mountains ────────────────────────────────────────────────────────────────────────────────────

def draw_mountains(ax, poster, theme: dict, s: float, proj, route_xy: np.ndarray, lay: Layout) -> None:
    """Overlapping ink peaks, shaded with short strokes down their right-hand slopes, placed on the
    glaciers in the map data and on named peaks (Poster.peaks), biggest first, never on the route.
    Drawn back to front so nearer peaks hide the ones behind them."""
    W, H = poster.frame.width, poster.frame.height
    sites = []  # (weight 0..1, x, y)
    for ring in poster.features.get("glacier", []):
        xy = proj(ring)
        area = abs(np.dot(xy[:-1, 0], xy[1:, 1]) - np.dot(xy[1:, 0], xy[:-1, 1])) / 2
        sites.append((min(area / 0.015, 1.0), *xy.mean(axis=0)))
    for lon, lat, ele in poster.peaks:
        x, y = poster.frame.project(lon, lat)
        sites.append((0.4 + 0.6 * min((ele - 1000) / 3000, 1.0), float(x), float(y)))  # visible even at 1,000 m
    sites.sort(key=lambda site: -site[0])

    x0, x1 = lay.inner + 0.15 * s, W - lay.inner - 0.15 * s
    y0, y1 = lay.map_bottom + 0.15 * s, H - lay.inner - 0.15 * s
    near = route_xy[:: max(1, len(route_xy) // 2000)]
    rng = np.random.default_rng(7)  # the same poster always gets the same mountains
    peaks = []
    for weight, x, y in sites:
        if not (x0 < x < x1 and y0 < y < y1):
            continue
        if np.hypot(*(near - (x, y)).T).min() < MOUNTAIN_ROUTE_GAP * s:
            continue
        if any(np.hypot(x - px, y - py) < MOUNTAIN_SPACING * s for px, py, _ in peaks):
            continue
        peaks.append((x, y, 0.11 * s * (1 + 0.9 * weight) * rng.uniform(0.85, 1.15)))
        if len(peaks) == MAX_MOUNTAINS:
            break

    ink, paper = theme["text"], theme["bg"]
    for i, (x, y, h) in enumerate(sorted(peaks, key=lambda p: -p[1])):  # far (top) first
        w = h * rng.uniform(1.25, 1.6)
        left, top, right = (x - w / 2, y), (x + rng.uniform(-0.12, 0.08) * w, y + h), (x + w / 2, y)
        z = 2.8 + i * 1e-5
        ax.add_collection(PolyCollection([[left, top, right]], facecolors=paper, edgecolors="none", zorder=z))
        strokes = [[left, top, right]]
        for t in np.linspace(0.18, 0.82, int(rng.integers(3, 6))):
            px, py = top[0] + (right[0] - top[0]) * t, top[1] + (right[1] - top[1]) * t
            length = (1 - t) * h * 0.75
            strokes.append([(px, py), (px - length * 0.35, py - length)])
        ax.add_collection(LineCollection(strokes, colors=ink, linewidths=0.75 * s, capstyle="round",
                                         joinstyle="round", zorder=z + 5e-6))


# ── frame, title block and notes ─────────────────────────────────────────────────────────────────

def decorate(fig, ax, poster, theme: dict, fonts: dict, s: float, lay: Layout, placed: list) -> None:
    """Everything drawn over the map: aged edges, the frame with its runes, the compass, the title
    block and the translation note. `placed` holds the boxes labels already use."""
    W, H = poster.frame.width, poster.frame.height
    ink, paper, accent = theme["text"], theme["bg"], theme.get("accent", theme["route"])
    border = poster.border or BorderText(poster.text.title, "")
    left = border.left or theme.get("runes_left", "")
    right = border.right or theme.get("runes_right", "")

    _vignette(ax, W, H)
    o, i = lay.outer, lay.inner
    for x, y, w, h in [(0, 0, W, i), (0, H - i, W, i), (0, 0, i, H), (W - i, 0, i, H), (i, i, W - 2 * i, lay.map_bottom - i)]:
        ax.add_patch(plt.Rectangle((x, y), w, h, facecolor=paper, edgecolor="none", zorder=9.5))
    for inset, lw in ((o, 2.2), (i, 0.9)):
        ax.add_patch(plt.Rectangle((inset, inset), W - 2 * inset, H - 2 * inset, fill=False, edgecolor=ink,
                                   linewidth=lw * s, zorder=9.7))
    for dy, lw in ((0, 0.9), (0.06 * s, 0.45)):
        ax.plot([i, W - i], [lay.map_bottom + dy] * 2, color=ink, lw=lw * s, zorder=9.7)

    band = (o + i) / 2
    _rune_line(ax, fonts, border.top, W / 2, H - band, 0, W - 2 * i - 0.6 * s, accent, s)
    _rune_line(ax, fonts, border.bottom, W / 2, band, 0, W - 2 * i - 0.6 * s, accent, s)
    _rune_line(ax, fonts, left, band, H / 2, 90, H - 2 * i - 0.6 * s, accent, s)
    _rune_line(ax, fonts, right, W - band, H / 2, -90, H - 2 * i - 0.6 * s, accent, s)

    compass = (W - i - 0.95 * s, H - i - 1.05 * s, 0.58 * s)
    _compass(ax, *compass, ink, accent, paper, fonts, s)
    cx, cy, r = compass
    placed.append((cx - r, cy - r, cx + r, cy + r + 0.3 * s))

    _title_block(fig, ax, poster, lay, ink, accent, fonts, s)
    _note(ax, lay, placed, border, left, right, ink, paper, fonts, s, W, H)


def _vignette(ax, W: float, H: float) -> None:
    """Edges darkened a little, like an old sheet of paper."""
    yy, xx = np.mgrid[-1:1:240j, -1:1:180j]
    d = np.clip((np.maximum(np.abs(xx), np.abs(yy)) ** 2 + np.hypot(xx, yy) ** 2 / 2 - 0.55) / 0.95, 0, 1)
    img = np.empty((240, 180, 4))
    img[..., :3] = mcolors.to_rgb(VIGNETTE_COLOR)
    img[..., 3] = d ** 2 * VIGNETTE_ALPHA
    ax.imshow(img, extent=(0, W, 0, H), origin="lower", aspect="auto", interpolation="bilinear", zorder=9.3)


def _rune_line(ax, fonts: dict, english: str, x: float, y: float, rotation: float, room: float,
               color: str, s: float) -> None:
    """One line of the border in runes, centred at (x, y), shrunk (down to 8 pt) to fit `room`
    inches; still too long, words are dropped from the end."""
    text = to_runes(english.strip())
    if not text:
        return
    fp = fonts["runes"].copy()
    for pt in np.arange(13 * s, 8 * s - 0.01, -0.5 * s):
        fp.set_size(pt)
        if _width_in(text, fp) <= room:
            break
    while _width_in(text, fp) > room and " " in text:
        text = text.rsplit(" ", 1)[0]
    ax.text(x, y, text, rotation=rotation, ha="center", va="center", color=color, fontproperties=fp,
            rotation_mode="anchor", zorder=9.8)


def _width_in(text: str, fp) -> float:
    """How wide `text` is in inches when drawn in font `fp`."""
    return TextPath((0, 0), text, prop=fp).get_extents().width / 72


def _compass(ax, cx: float, cy: float, r: float, ink: str, north: str, paper: str, fonts: dict, s: float) -> None:
    """An eight-point compass rose on a paper disc, north in the accent colour."""
    ax.add_patch(plt.Circle((cx, cy), r + 0.1 * s, facecolor=paper, edgecolor=ink, linewidth=0.7 * s, alpha=0.92, zorder=9.85))
    ax.add_patch(plt.Circle((cx, cy), r - 0.18 * s, fill=False, edgecolor=ink, linewidth=0.45 * s, zorder=9.86))
    for angle, length in [(0, r), (90, r), (180, r), (270, r), (45, 0.55 * r), (135, 0.55 * r), (225, 0.55 * r), (315, 0.55 * r)]:
        a = np.radians(90 - angle)
        tip = (cx + length * np.cos(a), cy + length * np.sin(a))
        w = (0.09 if length == r else 0.06) * s
        side_l = (cx + w * np.cos(a + np.pi / 2), cy + w * np.sin(a + np.pi / 2))
        side_r = (cx + w * np.cos(a - np.pi / 2), cy + w * np.sin(a - np.pi / 2))
        color = north if angle == 0 else ink
        ax.add_patch(plt.Polygon([(cx, cy), side_l, tip], facecolor=color, edgecolor=color, linewidth=0.5 * s, zorder=9.9))
        ax.add_patch(plt.Polygon([(cx, cy), side_r, tip], facecolor=paper, edgecolor=color, linewidth=0.7 * s, zorder=9.9))
    fp = fonts["bold"].copy()
    fp.set_size(15 * s)
    ax.text(cx, cy + r + 0.14 * s, "N", ha="center", va="bottom", color=north, fontproperties=fp, zorder=9.9)


def _title_block(fig, ax, poster, lay: Layout, ink: str, accent: str, fonts: dict, s: float) -> None:
    """The title in runes, the title, subtitle and facts, then a footer: scale bar on the left,
    dates and position in the middle, the OpenStreetMap credit on the right."""
    W, H = poster.frame.width, poster.frame.height
    text, y, size = poster.text, lay.y, lay.size

    def put(content: str, key: str, role: str, color: str, alpha: float = 1.0) -> None:
        fp = fonts[role].copy()
        fp.set_size(size[key])
        fig.text(0.5, y[key] / H, content, ha="center", color=color, alpha=alpha, fontproperties=fp, zorder=11)

    put(to_runes(text.title), "title_runes", "runes", accent)
    put(text.title, "title", "title", ink)
    if text.subtitle:
        put(text.subtitle, "subtitle", "italic", ink)
    if text.stats:
        put(text.stats, "stats", "bold", accent)
    put(text.footer, "footer", "italic", ink, alpha=0.85)

    fp = fonts["italic"].copy()
    fp.set_size(10 * s)
    ax.text(W - lay.inner - 0.3 * s, y["footer"], "© OpenStreetMap contributors", ha="right", va="baseline",
            color=ink, alpha=0.8, fontproperties=fp, zorder=11)
    if poster.show.scale_bar:
        _scale_bar(ax, poster.frame, lay, ink, fonts, s)


def _scale_bar(ax, frame: Frame, lay: Layout, ink: str, fonts: dict, s: float) -> None:
    """A two-part scale bar (filled, open) labelled in kilometres and, roughly, leagues."""
    km_per_inch = KM_PER_DEG / frame.k
    km = max((n for n in NICE_KM if n / km_per_inch <= 1.6 * s), default=NICE_KM[0])
    half = km / km_per_inch / 2
    x, y, h = lay.inner + 0.34 * s, lay.y["footer"] + 0.22 * s, 0.08 * s
    ax.add_patch(plt.Rectangle((x, y), half, h, facecolor=ink, edgecolor=ink, linewidth=0.7 * s, zorder=11))
    ax.add_patch(plt.Rectangle((x + half, y), half, h, fill=False, edgecolor=ink, linewidth=0.7 * s, zorder=11))
    leagues = max(1, round(km / LEAGUE_KM))
    fp = fonts["bold"].copy()
    fp.set_size(11 * s)
    label = f"{km} km · about {number_words(leagues)} league{'s' if leagues != 1 else ''}"
    ax.text(x, lay.y["footer"], label, ha="left", va="baseline", color=ink, fontproperties=fp, zorder=11)


def _note(ax, lay: Layout, placed: list, border: BorderText, left: str, right: str, ink: str, paper: str,
          fonts: dict, s: float, W: float, H: float) -> None:
    """A short italic note on the map saying what the runes read, in the first map corner free of
    labels: bottom left, bottom right, then top left. Skipped when all three are taken."""
    parts = [(where, _plain(what)) for where, what in
             [("above", border.top), ("below", border.bottom), ("on the left", left), ("on the right", right)] if what]
    if not parts:
        return
    first, *rest = parts
    note = f"The runes about the border read, {first[0]}: {first[1]}." + "".join(f" {where.capitalize()}: {what}." for where, what in rest)
    size = 11.5 * s
    lines = textwrap.wrap(note, 46)
    w, h, pad = 3.3 * s, len(lines) * size / 72 * 1.4 + 0.24 * s, 0.3 * s
    top = H - lay.inner - pad - h
    for x, y in ((lay.inner + pad, lay.map_bottom + pad), (W - lay.inner - pad - w, lay.map_bottom + pad), (lay.inner + pad, top)):
        box = (x, y, x + w, y + h)
        if any(box[0] < p[2] and p[0] < box[2] and box[1] < p[3] and p[1] < box[3] for p in placed):
            continue
        fp = fonts["italic"].copy()
        fp.set_size(size)
        ax.text(x + 0.12 * s, y + h - 0.12 * s, "\n".join(lines), ha="left", va="top", color=ink, linespacing=1.4,
                fontproperties=fp, zorder=9.9,
                bbox={"boxstyle": "square,pad=0.5", "facecolor": paper, "edgecolor": "none", "alpha": 0.88})
        placed.append(box)
        return


def _plain(english: str) -> str:
    """Border text as it reads in the note: 'Manali · Kaza' -> 'Manali, Kaza'."""
    return english.replace(" · ", ", ").strip()
