"""Themes (colours) and fonts. Both ship with the project: themes/*.json and fonts/jost/*.ttf."""
import json

from matplotlib.font_manager import FontProperties

from .errors import UserError
from .net import PROJECT_ROOT

THEME_DIR = PROJECT_ROOT / "themes"
FONT_DIR = PROJECT_ROOT / "fonts" / "jost"
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


def load_fonts() -> dict[str, FontProperties]:
    """The poster fonts by role: light, regular, bold, and italic (river and lake names)."""
    files = {"light": "jost-300.ttf", "regular": "jost-400.ttf", "bold": "jost-700.ttf", "italic": "jost-300italic.ttf"}
    return {role: FontProperties(fname=str(FONT_DIR / name)) for role, name in files.items()}
