"""Make the final poster of one trip in every theme, at one or more sizes.

    .venv/bin/python scripts/all_themes.py trips/spiti.yaml 12x16 instagram

Files go to posters/<trip>_<theme>_<size>.<format>, like `routetoposter make`.
"""
import sys
import time
from pathlib import Path

from routetoposter.cli import draw, read
from routetoposter.errors import UserError
from routetoposter.style import list_themes
from routetoposter.trip import SIZES


def main(trip_path: str, sizes: list[str]) -> None:
    for size in sizes:
        if size not in SIZES:
            sys.exit(f"Unknown size {size!r}. Choose from: {', '.join(SIZES)}")
    themes = list_themes()
    for size in sizes:
        start = time.time()
        trip = read(trip_path)
        trip.size = size
        print(f"\n=== {size}: {len(themes)} themes ===")
        try:
            draw(trip, Path(trip_path), themes, preview=False)  # builds the poster once, then one file per theme
        except UserError as e:
            print(f"✗ {size} skipped: {e}")
            continue
        print(f"=== {size} done in {time.time() - start:.0f}s ===")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
