"""Find the user's `sights_nearby` in OpenStreetMap.

Each sight is searched by name only within SEARCH_RADIUS_KM of its own stop. That's a narrow
search around a known point, so "Fort Kochi" can't turn up in another state. A sight given with
lat/lon is used as is.

OpenStreetMap often uses the local word: "Ki Gompa", not "Key Monastery". So words with the same
meaning (SAME_MEANING) are searched together, and when nothing is found, the error suggests the
closest names that do exist near the stop.
"""
import difflib
import math
import re
from dataclasses import dataclass

from .errors import UserError
from .extras import Sight, kind_from_tags
from .mapdata import overpass, printable_name
from .net import cached_json, is_cached
from .route import haversine_km
from .trip import NearbySight, Stop

SEARCH_RADIUS_KM = 25
KM_PER_DEG = 111.32
LOOKUP_VERSION = 2  # part of every cache key; bump when the search changes
# Words that mean the same thing in place names; the first word of each group is the one names are
# compared with. Matching is by contained text, so "tal" also finds "Chandratal".
SAME_MEANING = [
    ("monastery", "gompa", "gonpa", "gompha"),
    ("temple", "mandir", "kovil", "koil"),
    ("lake", "tal", "taal", "tso", "sarovar"),
    ("fort", "qila", "kila", "killa"),
    ("waterfall", "waterfalls", "falls", "fall"),
    ("palace", "mahal"),
    ("mosque", "masjid"),
    ("church", "chapel"),
    ("peak", "mount", "mt"),
]
MATCH_MIN_SIMILARITY = 0.7  # 0..1: how close a found name must be to the wanted one to be used
SUGGESTIONS = 3  # at most this many "did you mean" names
SUGGEST_MIN_SIMILARITY = 0.45  # 0..1: how close a name must be to be suggested
IGNORED_WORDS = {"the", "of", "and", "near"}  # too common to help find a suggestion


@dataclass
class Found:
    """What a lookup found for one sight, for `check` to print."""
    stop: str
    wanted: str
    sight: Sight
    how: str  # "your lat/lon", or e.g. "OpenStreetMap, 2.3 km from Kochi"
    others: list[str]  # other matches that weren't used


def locate_sights(stops: list[Stop]) -> list[Found]:
    """Every sights_nearby entry of every stop, located.

    - A name OpenStreetMap doesn't have near its stop is a mistake to fix: one UserError lists all
      of them, so they can be fixed in one go.
    - A lookup that fails because the server is down isn't a mistake: that sight is skipped with a
      warning and looked up again on the next run (failures are never cached)."""
    found, missing = [], []
    for stop in stops:
        for wish in stop.sights_nearby:
            if wish.lat is None and not is_cached("sight-search", search_key(wish.name, stop.lat, stop.lon)):
                print(f"     looking up '{wish.name}' near {stop.name}…", flush=True)
            try:
                result = locate(wish, stop)
            except UserError as e:
                print(f"     ⚠ Couldn't look up '{wish.name}' ({str(e).splitlines()[0]}); the poster is made\n"
                      "       without it. Run it again later to add it.")
                continue
            if result is None:
                missing.append(not_found_line(wish.name, stop))
            else:
                found.append(result)
    if missing:
        raise UserError(
            f"Couldn't find these sights within {SEARCH_RADIUS_KM} km of their stop in OpenStreetMap:\n    "
            + "\n    ".join(missing)
            + "\n  Use one of the suggested names, check the spelling, add lat/lon\n"
              "  (e.g. {name: Cherai Beach, lat: 10.14, lon: 76.18}, copied from Google Maps), or remove it.")
    return found


def locate(wish: NearbySight, stop: Stop) -> Found | None:
    """Where one sight is, or None when OpenStreetMap has nothing by that name near the stop."""
    home = (stop.lon, stop.lat)
    if wish.lat is not None:
        sight = Sight(wish.name, wish.lon, wish.lat, wish.kind or "sight", stop=home)
        return Found(stop.name, wish.name, sight, "your lat/lon", [])

    matches = search(wish.name, stop.lat, stop.lon)
    # Containing the words isn't enough ("Scalinata per Key Gompa" is a staircase near Key Gompa):
    # keep matches whose whole name is close to the wanted one, the most similar first, then the
    # closest to the stop. None left means not found, and the error then suggests names.
    wanted = normalise(wish.name)
    scored = [(similarity(wanted, normalise(m["name"])), haversine_km(home, (m["lon"], m["lat"])), m) for m in matches]
    scored = sorted((s for s in scored if s[0] >= MATCH_MIN_SIMILARITY), key=lambda s: (-s[0], s[1]))
    if not scored:
        return None
    matches = [m for _, _, m in scored]
    best = matches[0]
    km = haversine_km(home, (best["lon"], best["lat"]))
    sight = Sight(wish.name, best["lon"], best["lat"], wish.kind or best["kind"], stop=home)
    others = [f"{m['name']} ({haversine_km(home, (m['lon'], m['lat'])):.1f} km)" for m in matches[1:4]]
    return Found(stop.name, wish.name, sight, f"OpenStreetMap '{best['name']}', {km:.1f} km from {stop.name}", others)


def search(name: str, lat: float, lon: float) -> list[dict]:
    """OpenStreetMap places whose name contains `name` (ignoring case, and accepting same-meaning
    words: "Key Monastery" also finds "Key Gompa") near (lat, lon): [{name, lon, lat, kind}]. Cached."""
    return cached_json("sight-search", search_key(name, lat, lon),
                       lambda: _places_named(name_pattern(name), lat, lon))


def search_key(name: str, lat: float, lon: float) -> list:
    """The cache key of one search."""
    return [name.strip().casefold(), round(lat, 4), round(lon, 4), SEARCH_RADIUS_KM, LOOKUP_VERSION]


def not_found_line(name: str, stop: Stop) -> str:
    """"'Key Monastery' near Kaza: did you mean Ki Gompa (7.9 km)?" (without the question when there
    is nothing close, or the suggestion lookup itself fails)."""
    try:
        names = suggest(name, stop.lat, stop.lon)
    except UserError:
        names = []
    line = f"{name!r} near {stop.name}"
    return f"{line}: did you mean {' or '.join(names)}?" if names else line


def suggest(name: str, lat: float, lon: float) -> list[str]:
    """Up to SUGGESTIONS names near (lat, lon) that look like `name`, e.g. ["Ki Gompa (7.9 km)"].
    Searches for places containing any one of the name's words (or their same-meaning words), then
    keeps the most similar names. Cached."""
    words = [w for w in name.split() if len(w) >= 3 and w.casefold() not in IGNORED_WORDS]
    alternatives = sorted({alt for w in words for alt in _same_meaning(w)})
    if not alternatives:
        return []
    pattern = "(" + "|".join(alternatives) + ")"
    key = ["suggest", name.strip().casefold(), round(lat, 4), round(lon, 4), SEARCH_RADIUS_KM, LOOKUP_VERSION]
    places = cached_json("sight-search", key, lambda: _places_named(pattern, lat, lon))
    wanted = normalise(name)
    scored = {}
    for p in places:
        score = similarity(wanted, normalise(p["name"]))
        if score >= SUGGEST_MIN_SIMILARITY and score > scored.get(p["name"], (0,))[0]:
            scored[p["name"]] = (score, haversine_km((lon, lat), (p["lon"], p["lat"])))
    best = sorted(scored.items(), key=lambda kv: -kv[1][0])[:SUGGESTIONS]
    return [f"{n} ({km:.1f} km)" for n, (_, km) in best]


def name_pattern(name: str) -> str:
    """The Overpass regex for a name: each word escaped, and a word with same-meaning words replaced
    by all of them: "Key Monastery" -> "Key (monastery|gompa|gonpa|gompha)"."""
    parts = []
    for word in name.split():
        group = _same_meaning(word)
        parts.append("(" + "|".join(group) + ")" if len(group) > 1 else group[0])
    return " ".join(parts)


def _same_meaning(word: str) -> list[str]:
    """The word's group from SAME_MEANING, or just the word itself (regex-escaped)."""
    for group in SAME_MEANING:
        if word.casefold() in group:
            return list(group)
    return [re.escape(word).replace('"', '\\"')]


def similarity(a: str, b: str) -> float:
    """How alike two names are, from 0 (nothing in common) to 1 (the same)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def normalise(name: str) -> str:
    """A name for comparing: lower case, same-meaning words replaced by their group's first word.
    'Ki Gompa' -> 'ki monastery'."""
    words = []
    for word in name.casefold().split():
        group = next((g for g in SAME_MEANING if word in g), None)
        words.append(group[0] if group else word)
    return " ".join(words)


def _places_named(pattern: str, lat: float, lon: float) -> list[dict]:
    """Places whose name matches `pattern` (case-insensitive) within a square of SEARCH_RADIUS_KM
    around (lat, lon). A square, not a circle: Overpass answers it several times faster. Roads and
    bus routes named after a place aren't the place, so they're left out."""
    d_lat = SEARCH_RADIUS_KM / KM_PER_DEG
    d_lon = d_lat / math.cos(math.radians(lat))
    bbox = f"{lat - d_lat:.4f},{lon - d_lon:.4f},{lat + d_lat:.4f},{lon + d_lon:.4f}"
    query = f'[out:json][timeout:120];nwr["name"~"{pattern}",i]({bbox});out center tags;'
    rows = []
    for el in overpass(query, patient=False)["elements"]:
        tags = el.get("tags", {})
        pos = el.get("center") or el
        if "lat" in pos and "highway" not in tags and "route" not in tags:
            rows.append({"name": printable_name(tags) or tags.get("name", ""), "lon": pos["lon"],
                         "lat": pos["lat"], "kind": kind_from_tags(tags)})
    return rows
