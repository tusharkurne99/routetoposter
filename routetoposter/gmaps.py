"""Read the stops out of a Google Maps directions link.

A directions link looks like this (shortened):

    https://www.google.com/maps/dir/Manali,+Himachal+Pradesh/Kaza,+Himachal+Pradesh+172114/@31.8,76.6,8z/
        data=!4m20!4m19!1m5!1m1!1s0x39..!2m2!1d77.1891761!2d32.2431872!1m5 ... !3e0

- The path between `/dir/` and `/@` holds one segment per stop: the place's full name.
- `data=` holds, for every stop picked from Google's suggestions, `!1d<longitude>!2d<latitude>`,
  in the same order as the names.
- `!3e<n>` is the travel mode: 0 drive, 1 bicycle, 2 walk, 3 transit, 4 flight.

The road line itself is not in the link (Google recomputes it when the link is opened), so this
module only returns the stops and the travel mode; route.py draws the road line.
"""
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlparse

from .errors import UserError
from .net import USER_AGENT
from .trip import Stop

DIR_MARKER = "/maps/dir/"
SHORT_LINK_HOSTS = {"maps.app.goo.gl", "goo.gl"}
COORD_RE = re.compile(r"!1d(-?\d+(?:\.\d+)?)!2d(-?\d+(?:\.\d+)?)")  # !1d<lon>!2d<lat>
TYPED_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")  # "12.97,77.59"
MODE_RE = re.compile(r"!3e(\d)")
MODES = {"0": "driving", "1": "bicycling", "2": "walking", "3": "transit", "4": "flight"}
# Words at the end of business listings that make a poster label long without saying where you were.
LISTING_WORDS = ("post office", "home stay", "homestay", "guest house", "outpost", "hotel", "resort", "camp")


@dataclass
class ParsedLink:
    url: str  # the full link (a short link is replaced by the one it points to)
    stops: list[Stop]
    mode: str  # one of MODES' values


def parse_link(url: str) -> ParsedLink:
    """The stops and travel mode in a Google Maps directions link. Raises UserError when the link
    isn't a directions link or a stop's position can't be read from it."""
    url = resolve_short_link(url.strip())
    path = urlparse(url).path
    if DIR_MARKER not in path:
        raise UserError("That isn't a Google Maps directions link.\n"
                        "  Open the route in Google Maps (with all stops added) and copy the address bar;\n"
                        "  it should contain /maps/dir/.")

    names = stop_names(path)
    if len(names) < 2:
        raise UserError("The link has fewer than two stops.")

    coords = [(float(lat), float(lon)) for lon, lat in COORD_RE.findall(url)]
    picked = [n for n in names if not TYPED_COORD_RE.match(n)]  # stops chosen from Google's suggestions
    if picked and "data=" in url and "!" not in url.split("data=", 1)[1]:
        raise UserError(
            "The link's stop positions are missing: the part after data= has been changed. This is\n"
            "  almost always the shell: in bash, ! inside \"double quotes\" is replaced from your command\n"
            "  history. Put the link in 'single quotes', or run without the link and paste it when asked:\n"
            "    routetoposter new -o trips/mytrip.yaml")
    if len(coords) != len(picked):
        raise UserError(
            f"The link has {len(picked)} named stops but positions for {len(coords)}, so they can't be\n"
            "  matched up safely. This happens when a stop was typed but not picked from Google's\n"
            "  suggestions, or when the route line was dragged (not supported yet). In Google Maps,\n"
            "  re-pick each stop from the suggestion list, don't drag the line, and copy the link again.")

    stops, positions = [], iter(coords)
    for full in names:
        typed = TYPED_COORD_RE.match(full)
        if typed:
            lat, lon = float(typed.group(1)), float(typed.group(2))
            stops.append(Stop(name=f"{lat:.4f}, {lon:.4f}", lat=lat, lon=lon, link_name=full))
        else:
            lat, lon = next(positions)
            stops.append(Stop(name=clean_name(full), lat=lat, lon=lon, link_name=full))

    mode = MODE_RE.search(url)
    return ParsedLink(url=url, stops=stops, mode=MODES.get(mode.group(1), "driving") if mode else "driving")


def resolve_short_link(url: str) -> str:
    """A maps.app.goo.gl link redirects to the full link; follow it once. Other links are returned
    unchanged."""
    if (urlparse(url).hostname or "") not in SHORT_LINK_HOSTS:
        return url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.geturl()


def stop_names(path: str) -> list[str]:
    """The decoded path segments between `/maps/dir/` and the `@` map position (or `data=`)."""
    names = []
    for segment in path.split(DIR_MARKER, 1)[1].split("/"):
        if segment.startswith("@") or segment.startswith("data="):
            break
        if segment:
            names.append(unquote_plus(segment))
    return names


def clean_name(full: str) -> str:
    """A short poster label from Google's full place name:
    'Kaza, Himachal Pradesh 172114' -> 'Kaza'; 'Shoja Valley Home Stay, H99C+M69, …' -> 'Shoja Valley'."""
    name = full.split(",")[0].split(" - ")[0].strip()
    changed = True
    while changed:
        changed = False
        for word in LISTING_WORDS:
            if name.lower().endswith(" " + word):
                name = name[: -len(word) - 1].strip()
                changed = True
    return name
