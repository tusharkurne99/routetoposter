"""The command line: new, check, preview, make, themes.

    routetoposter new ['<google maps link>'] [-o trip.yaml]   (no link: it asks you to paste it)
    routetoposter check trip.yaml
    routetoposter preview trip.yaml [--all-themes]
    routetoposter make trip.yaml
    routetoposter themes
"""
import argparse
import re
import sys
import urllib.error
from pathlib import Path

from .build import build_poster
from .errors import UserError
from .gmaps import parse_link
from .layout import output_dpi
from .net import PROJECT_ROOT
from .render import render
from .route import describe, route_trip
from .sheet import contact_sheet
from .sights import locate_sights
from .style import list_themes, load_fonts, load_theme
from .trip import Trip, load_trip, write_trip

POSTER_DIR = PROJECT_ROOT / "posters"


def main(argv: list[str] | None = None) -> int:
    """Run a command. Problems the user can fix are printed as one short message (exit code 1)."""
    sys.stdout.reconfigure(line_buffering=True)  # show progress lines at once, even in a log file
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except UserError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"\n✗ File not found: {e.filename}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"\n✗ Couldn't reach an online map service ({getattr(e, 'reason', e)}).\n"
              "  Check the internet connection and run it again; everything downloaded so far is saved.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Everything downloaded so far is saved; the next run continues from there.", file=sys.stderr)
        return 130


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="routetoposter", description="Turn a Google Maps road trip into a map poster.")
    sub = p.add_subparsers(required=True, metavar="command")

    new = sub.add_parser("new", help="make a trip file from a Google Maps directions link")
    new.add_argument("link", nargs="?", help="the Google Maps directions link, in 'single quotes' "
                     "(leave it out to be asked for it, which avoids quoting problems)")
    new.add_argument("-o", "--output", help="trip file to write (default: named after the start and end)")
    new.set_defaults(run=cmd_new)

    check = sub.add_parser("check", help="check a trip file and find its sights, without drawing")
    check.add_argument("trip")
    check.set_defaults(run=cmd_check)

    preview = sub.add_parser("preview", help="quick low-resolution poster into posters/previews/")
    preview.add_argument("trip")
    preview.add_argument("--all-themes", action="store_true", help="one preview per theme, plus a sheet with all of them")
    preview.set_defaults(run=cmd_preview)

    make = sub.add_parser("make", help="the final poster")
    make.add_argument("trip")
    make.set_defaults(run=cmd_make)

    themes = sub.add_parser("themes", help="list the themes")
    themes.set_defaults(run=cmd_themes)
    return p


def cmd_new(args) -> int:
    """Link -> trip file. Routes the trip once so the file can list every leg's distance."""
    link = parse_link(args.link or ask_for_link())
    if link.mode not in ("driving", "flight"):
        print(f"Note: the link is for {link.mode}; the route is drawn along driving roads.")
    trip = Trip(stops=link.stops, link=link.url, title=default_title(link.stops),
                route="straight" if link.mode == "flight" else "driving")
    print(f"Found {len(trip.stops)} stops. Working out the route…")
    route = route_trip(trip.stops, trip.route)
    path = Path(args.output) if args.output else Path(f"{slug(trip.title)}.yaml")
    if path.exists():
        raise UserError(f"{path} already exists. Pick another name with -o, or delete it first.")
    text = write_trip(trip, path, describe(route, trip.stops), list_themes())
    print(f"\n{text}\nSaved {path}. Edit it (nights, title, dates, theme, size…), then run:\n"
          f"  routetoposter preview {path}\n  routetoposter make {path}")
    return 0


def cmd_check(args) -> int:
    """Validate the trip file and show where each sight was found."""
    trip = read(args.trip)
    print(f"{args.trip} is valid: {len(trip.stops)} stops, theme {trip.theme}, size {trip.size}.")
    for f in locate_sights(trip.stops):
        print(f"  ● {f.wanted} near {f.stop}: {f.how}" + (f"\n      also matched: {', '.join(f.others)}" if f.others else ""))
    return 0


def cmd_preview(args) -> int:
    trip = read(args.trip)
    themes = list_themes() if args.all_themes else [trip.theme]
    saved = draw(trip, Path(args.trip), themes, preview=True)
    if len(saved) > 1:
        sheet = contact_sheet([(name, path) for name, path in saved], POSTER_DIR / "previews" / f"{Path(args.trip).stem}_all_themes.png")
        print(f"Saved {sheet} (all themes side by side)")
    print(f"\nLike it? Make the final poster with:\n  routetoposter make {args.trip}")
    return 0


def cmd_make(args) -> int:
    trip = read(args.trip)
    draw(trip, Path(args.trip), [trip.theme], preview=False)
    return 0


def cmd_themes(args) -> int:
    for name in list_themes():
        print(f"{name:16s} {load_theme(name).get('description', '')}")
    return 0


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────

def read(path: str) -> Trip:
    return load_trip(Path(path), list_themes())


def draw(trip: Trip, trip_path: Path, themes: list[str], preview: bool) -> list[tuple[str, Path]]:
    """Build the poster once and draw it in each theme. Returns [(theme, saved file)].
    Files: posters/<trip file name>_<theme>_<size>.<format>, or for previews
    posters/previews/<trip file name>_<theme>.png. Each run overwrites the previous file."""
    poster, _ = build_poster(trip)
    fonts = load_fonts()
    out_dir = POSTER_DIR / "previews" if preview else POSTER_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    dpi = output_dpi(trip, poster.frame.width, poster.frame.height, preview)
    print(f"4/4  Drawing {len(themes)} posters" if len(themes) > 1 else "4/4  Drawing")
    saved = []
    for name in themes:
        if preview:
            path = out_dir / f"{trip_path.stem}_{name}.png"
        else:
            path = out_dir / f"{trip_path.stem}_{name}_{trip.size}.{trip.format}"
        render(str(path), poster, load_theme(name), fonts, dpi)
        print(f"Saved {path}")
        saved.append((name, path))
    return saved


def ask_for_link() -> str:
    """Ask for the link on the keyboard. Pasted here, the shell never sees it, so its `!` and `&`
    characters can't be changed (see gmaps.parse_link)."""
    try:
        return input("Paste the Google Maps directions link and press Enter:\n> ").strip()
    except EOFError:
        raise UserError("No link given.")


def default_title(stops) -> str:
    """A placeholder title the user is expected to change: the start, or "Start to End"."""
    first, last = stops[0].name, stops[-1].name
    return first if first == last else f"{first} to {last}"


def slug(text: str) -> str:
    """'Kerala, end of the year!' -> 'kerala_end_of_the_year'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "trip"
