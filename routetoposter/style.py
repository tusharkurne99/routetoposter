"""Themes (colours) and fonts. Both ship with the project: themes/*.json and fonts/<family>/*.ttf."""
import json

from matplotlib.font_manager import FontProperties

from .errors import UserError
from .net import PROJECT_ROOT

THEME_DIR = PROJECT_ROOT / "themes"
FONT_ROOT = PROJECT_ROOT / "fonts"
DEFAULT_FONTS = {"light": "jost/jost-300.ttf", "regular": "jost/jost-400.ttf", "bold": "jost/jost-700.ttf",
                 "italic": "jost/jost-300italic.ttf"}
REQUIRED_COLOURS = ["bg", "text", "water", "parks", "road_motorway", "road_primary", "road_secondary",
                    "road_tertiary", "road_residential", "road_default"]


def list_themes() -> list[str]:
    """Theme names: the file names in themes/ without .json."""
    return sorted(p.stem for p in THEME_DIR.glob("*.json"))


def load_theme(name: str) -> dict:
    """A theme's colours. Themes are maptoposter's files plus two keys: `route` (the route colour)
    and `water_line` (rivers, for themes whose water colour is close to the background); both fall
    back to sensible colours when missing."""
    path = THEME_DIR / f"{name}.json"
    if not path.exists():
        raise UserError(f"Unknown theme {name!r}. Available: {', '.join(list_themes())}")
    theme = json.loads(path.read_text())
    missing = [k for k in REQUIRED_COLOURS if k not in theme]
    if missing:
        raise UserError(f"Theme {name!r} is missing colours: {', '.join(missing)}")
    theme.setdefault("gradient_color", theme["bg"])
    theme.setdefault("route", theme["text"])
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
        fonts[role] = FontProperties(fname=str(path))
    return fonts
