# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

RouteToPoster: a Python CLI that turns a Google Maps directions link into a maptoposter-style map
poster (real OSM roads/rivers/lakes in a theme, with route, stops, mountain passes and sights on top).
Flow: `new` parses the link and writes an editable YAML trip file → user edits it → `preview` / `make`
draw the poster.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup (Python >= 3.10)

.venv/bin/pytest                                              # all tests (offline, ~1s)
.venv/bin/pytest tests/test_map_and_poster.py::test_render_offline   # single test

.venv/bin/routetoposter new -o trips/x.yaml       # asks for the link (avoids shell quoting of ! and &)
.venv/bin/routetoposter check trips/x.yaml        # validate trip file, locate sights
.venv/bin/routetoposter preview trips/x.yaml [--all-themes]   # -> posters/previews/
.venv/bin/routetoposter make trips/x.yaml         # -> posters/<trip>_<theme>_<size>.<format>
.venv/bin/routetoposter themes
.venv/bin/python scripts/all_themes.py trips/x.yaml 12x16 instagram   # every theme, final quality
```

`examples/*.yaml` are finished trip files to try (`cp examples/spiti.yaml trips/`). `trips/`,
`posters/`, `cache/` and `docs/` are gitignored. No linter/formatter is configured.

## Architecture

Pipeline (package `routetoposter/`):

- `gmaps.py` — parses the directions link: stop names from the `/dir/` path, coordinates from
  `!1d<lon>!2d<lat>` in `data=`, travel mode from `!3e<n>`. The road line is not in the link.
- `trip.py` — the trip file: dataclasses (`Trip`, `Stop`, `Show`, `NearbySight`), `write_trip`
  (emits the commented YAML) and `load_trip` (validation with "did you mean" suggestions). Also
  holds the option enums (`SIZES`, `SIGHT_KINDS`, …). Adding a trip setting means updating both
  the writer's comments and the loader.
- `route.py` — routing: each driving leg on its own (cached per leg) via OSRM; on failure or a stop
  snapped > `SNAP_LIMIT_M`, Valhalla → straight line, with warnings. Sea legs cached by their ends
  (`SAILING_VERSION`). Flags detour legs. A stop's own `route`
  (how you got there: flying/sailing/…) overrides the trip's for that leg; the result is cut into
  `Segment`s by mode (render draws each style; `Route.ground_lines()` feeds passes/altitudes/sights).
- `sailing.py` — sea legs round the land: water grid from the coastline, A*, turns rounded into curves.
  `off_map: true` on a stop: its legs become their own `Segment(off_map=True)`, left out of
  `Route.fit_coords()` (the frame) and `ground_lines()`; render clips them at the map edge and
  labels the exit point (`_on_map_part`, `_edge_label`).
- `layout.py` — poster size in inches and `Frame`: local projection of (lon, lat) to poster inches;
  route fits in `ROUTE_BOX`, bottom ~30% reserved for the title block.
- `mapdata.py` — Overpass downloads in tiles (with backup servers; `OVERPASS_SLOTS` at once; all-sea squares skipped), `download_level` /
  `roads_to_draw` choose detail by area; sea polygons.
- `extras.py` — passes, auto sights, peaks (Overpass) and altitudes (Open-Meteo).
- `sights.py` — finds user `sights_nearby` by name within `SEARCH_RADIUS_KM` of their stop, with
  synonym groups (gompa = monastery, tal/tso = lake…).
- `build.py` — the **only** orchestrator: calls the above in order, prints the `1/4…4/4` progress,
  builds a `Poster`. Optional lookups go through `optional()` so failures degrade to a warning.
- `render.py` — matplotlib drawing only, **no network**. Everything arrives in a `Poster`. Sizes
  are points on a 12x16 poster scaled by `s`; zorder layering documented in the module docstring.
- `pelennor_fields.py` + `runes.py` — themes with `"style": "pelennor_fields"` get frame, runes
  border, ink mountains, compass and their own title block from here instead of render.py's fades
  and spaced title (render.py still draws map/route/markers/labels).
- `style.py` — themes are `themes/*.json` (maptoposter format + optional `route`, `water_line`,
  `style`, `fonts`); fonts ship in `fonts/`. New theme = new JSON file with `REQUIRED_COLOURS`.
- `cli.py` — argparse subcommands. `sheet.py` — contact sheet for `--all-themes`.

Conventions:

- `errors.UserError` is the one exception meant for users: the CLI prints its message (no
  traceback) and exits 1. Messages are plain-language and actionable.
- All network goes through `net.py` (`fetch`, retries on 429/5xx) and its on-disk JSON cache
  (`cached_json(kind, key, produce)` → `cache/<kind>/<hash>.json`). Area lookups (coastline, passes,
  peaks, auto sights, road map) are cached per square of a fixed world grid (`net.grid_rows`,
  `mapdata.fetch_features`), so a shifted area only fetches new squares; `cache/areas.json` is the
  pre-grid per-area cache, still read via `old_area_rows`. Cache-key version constants
  (`POI_VERSION`, `LOOKUP_VERSION`, `FEATURES_VERSION`, `SAILING_VERSION`) must be bumped when results change.
  `$ROUTETOPOSTER_CACHE` overrides the cache dir.
- Tests never hit the network: use `tests/data/*_link.txt`, `route: "straight"`, monkeypatched
  lookups. `tests/conftest.py` gives every test an empty cache dir of its own.
- Style: dense docstrings explaining *why*, module-level tuned constants with comments, long lines
  (~120) are normal.
