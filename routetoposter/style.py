"""Themes (colours) and fonts. Both ship with the project: themes/*.json and fonts/<family>/*.ttf."""
import json
import logging

from matplotlib.font_manager import FontProperties, fontManager, ttfFontProperty
from matplotlib.ft2font import FT2Font

from .errors import UserError
from .net import PROJECT_ROOT
from .trip import ROUTE_MODES

THEME_DIR = PROJECT_ROOT / "themes"
FONT_ROOT = PROJECT_ROOT / "fonts"
DEFAULT_FONTS = {"light": "jost/jost-300.ttf", "regular": "jost/jost-400.ttf", "bold": "jost/jost-700.ttf",
                 "italic": "jost/jost-300italic.ttf"}
# Draws what a poster font has no glyph for (none of them has "→", which the auto subtitle uses). It
# ships with matplotlib. Each font is used by family name with this one after it: matplotlib then
# falls back glyph by glyph (a font given by file path gets no fallback).
FALLBACK_FONT = "DejaVu Sans"
# DejaVu Sans has no light (300) weight, so matplotlib logs "Failed to find font weight 300" on every
# run; it uses the regular weight for the odd fallback glyph, which is fine.
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
REQUIRED_COLOURS = ["bg", "text", "water", "parks", "road_motorway", "road_primary", "road_secondary",
                    "road_tertiary", "road_residential", "road_default"]


def list_themes() -> list[str]:
    """Theme names: the file names in themes/ without .json."""
    return sorted(p.stem for p in THEME_DIR.glob("*.json"))


def load_theme(name: str) -> dict:
    """A theme's colours. Themes are maptoposter's files plus a few keys: `route` (the route colour,
    also the stop markers), `route_driving` / `route_straight` / `route_flying` / `route_sailing`
    (that kind of leg, e.g. boat trips in the sea's colour; else `route`), and `water_line` (rivers,
    for themes whose water colour is close to the background); all fall back to sensible colours
    when missing."""
    path = THEME_DIR / f"{name}.json"
    if not path.exists():
        raise UserError(f"Unknown theme {name!r}. Available: {', '.join(list_themes())}")
    theme = json.loads(path.read_text())
    missing = [k for k in REQUIRED_COLOURS if k not in theme]
    if missing:
        raise UserError(f"Theme {name!r} is missing colours: {', '.join(missing)}")
    theme.setdefault("gradient_color", theme["bg"])
    theme.setdefault("route", theme["text"])
    for mode in ROUTE_MODES:
        theme.setdefault(f"route_{mode}", theme["route"])
    theme.setdefault("water_line", theme["water"])
    return theme


def load_fonts(theme: dict | None = None) -> dict[str, FontProperties]:
    """The poster fonts by role: light, regular, bold, italic (river and lake names), title (the big
    title; defaults to bold) and, for the Pelennor Fields style, runes. A theme's `fonts` (paths under fonts/)
    replace the default Jost files role by role."""
    files = dict(DEFAULT_FONTS)
    files.update((theme or {}).get("fonts", {}))
    files.setdefault("title", files["bold"])
    fonts = {}
    for role, name in files.items():
        path = FONT_ROOT / name
        if not path.exists():
            raise UserError(f"Font file {path} (role {role!r}) is missing.")
        fontManager.addfont(str(path))
        entry = ttfFontProperty(FT2Font(str(path)))  # its name, weight, style as the font manager has them
        fonts[role] = FontProperties(family=[entry.name, FALLBACK_FONT], weight=entry.weight, style=entry.style,
                                     stretch=entry.stretch)
    return fonts
