"""HTTP requests with retries, and an on-disk JSON cache so nothing is downloaded twice.

HTTPS is checked against the computer's own trusted certificates (truststore), not only Python's
bundled list: a company network that inspects traffic (Cato, Zscaler, Netskope…) signs every site
with its own certificate, which the computer trusts but Python's list doesn't.
"""
import contextvars
import hashlib
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path

from .errors import UserError

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # an install from before truststore was a dependency: Python's own list only
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "routetoposter/0.2 (road-trip poster generator)"
RETRY_STATUS = {429, 502, 503, 504}  # "busy" answers worth retrying
# Saved downloads never go stale by default: roads and coastlines change over years, not weeks.
# Set $ROUTETOPOSTER_CACHE_DAYS (e.g. 365) to download anything older again; 0 = keep forever.
CACHE_DAYS: float | None = None
CACHE_DAYS_ENV = "ROUTETOPOSTER_CACHE_DAYS"
# Where requests report what they're doing ("router.project-osrm.org didn't answer; retrying in 3s").
# build.py sets it around each step so its status line can show it; unset, nothing is reported.
NOTICE: ContextVar = ContextVar("notice", default=None)


def notice(text: str) -> None:
    """Tell the current status line (NOTICE), if there is one."""
    say = NOTICE.get()
    if say:
        say(text)


class CertificateProblem(UserError):
    """HTTPS failed because the site's certificate isn't trusted. Retrying never helps, so it's
    raised at once, and callers that fall back on another server let it through."""


def fetch(url: str, data: bytes | None = None, timeout: int = 180, retries: int = 2,
          content_type: str | None = None) -> bytes:
    """GET `url` (or POST `data` to it) and return the body. Busy servers and network hiccups are
    retried with growing pauses; any other HTTP error is raised straight away."""
    headers = {"User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError) as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise CertificateProblem(
                    f"Can't make a secure connection to {urllib.request.urlparse(url).hostname}: {e.reason.verify_message}.\n"
                    "  This usually means a company network or VPN is inspecting web traffic. Try off the "
                    "VPN, or\n  point SSL_CERT_FILE at a certificate file that includes your company's certificate.")
            last_error = e
        if attempt < retries:
            pause = 3 * (attempt + 1)
            notice(f"{urllib.request.urlparse(url).hostname} didn't answer ({last_error}); retrying in {pause}s")
            time.sleep(pause)
    raise last_error


def fetch_json(url: str, data: bytes | None = None, timeout: int = 180, retries: int = 2,
               content_type: str | None = None):
    """`fetch`, then parse the body as JSON."""
    return json.loads(fetch(url, data, timeout, retries, content_type))


def cache_dir() -> Path:
    """`cache/` in the project, or $ROUTETOPOSTER_CACHE (the tests point it at a temp folder)."""
    return Path(os.environ.get("ROUTETOPOSTER_CACHE", PROJECT_ROOT / "cache"))


def cache_path(kind: str, key) -> Path:
    """Where the value for `key` lives: cache/<kind>/<hash of key>.json. `key` is anything JSON can
    encode (a URL, a list of numbers…)."""
    digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    return cache_dir() / kind / f"{digest}.json"


def cache_max_days() -> float | None:
    """How many days a saved download counts as fresh: $ROUTETOPOSTER_CACHE_DAYS, else CACHE_DAYS
    (None or 0 = forever). Roads, coastlines and passes hardly change, so the default is forever."""
    value = os.environ.get(CACHE_DAYS_ENV, "").strip()
    if not value:
        return CACHE_DAYS
    try:
        days = float(value)
    except ValueError:
        raise UserError(f"{CACHE_DAYS_ENV} must be a number of days (0 = keep forever), not {value!r}.")
    return days if days > 0 else None


def is_fresh(path: Path) -> bool:
    """Is the file saved and younger than cache_max_days()? (Its age is its modification time: a
    download that is redone rewrites it.)"""
    if not path.exists():
        return False
    days = cache_max_days()
    return days is None or time.time() - path.stat().st_mtime < days * 86400


def is_cached(kind: str, key) -> bool:
    return is_fresh(cache_path(kind, key))


def cached_json(kind: str, key, produce):
    """The cached value for `key`; on a miss, call `produce()`, save what it returns, and return it.
    A saved value older than cache_max_days() is downloaded again, but if that fails the old one is
    used: old map data beats no poster."""
    path = cache_path(kind, key)
    if is_fresh(path):
        return json.loads(path.read_text())
    try:
        value = produce()
    except Exception as e:
        if not path.exists():
            raise
        notice(f"couldn't refresh saved {kind} ({str(e).splitlines()[0] if str(e) else type(e).__name__}); using the old copy")
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


# ── the world grid ───────────────────────────────────────────────────────────────────────────────
# Lookups over an area (coastline, passes, peaks, auto sights) are saved per square of a fixed world
# grid, not per poster area: moving a stop, adding one or another poster size shifts the area a
# little, and then only the squares it adds are downloaded. Trips that overlap share squares, and the
# sailing legs share the coastline squares with the map. (The road map does the same with its own
# squares: mapdata.fetch_features.)

Bbox = tuple[float, float, float, float]  # (south, west, north, east)


def grid_squares(bbox: Bbox, step: float) -> list[Bbox]:
    """The squares of the `step`-degree world grid that overlap the area, as (south, west, north, east)."""
    s, w, n, e = bbox
    rows = range(math.floor(s / step), math.ceil(n / step))
    cols = range(math.floor(w / step), math.ceil(e / step))
    return [(r * step, c * step, (r + 1) * step, (c + 1) * step) for r in rows for c in cols]


def grid_rows(kind: str, bbox: Bbox, step: float, version: list, fetch_square, slots: int = 1) -> list:
    """The rows `fetch_square(square)` returns for every grid square overlapping `bbox`, in grid
    order; squares not saved yet are fetched `slots` at a time. A feature crossing a square's edge can
    come back from both squares: dropping the copy is the caller's job. `version` goes in every key."""
    squares = grid_squares(bbox, step)

    def rows(square: Bbox) -> list:
        return cached_json(kind, [[round(v, 4) for v in square], step, *version], lambda: fetch_square(square))

    missing = [sq for sq in squares if not is_cached(kind, [[round(v, 4) for v in sq], step, *version])]
    if missing:
        with ThreadPoolExecutor(slots) as pool:
            # copy_context: the status line (NOTICE) reaches the requests made in the threads.
            futures = [pool.submit(contextvars.copy_context().run, rows, sq) for sq in missing]
            try:
                for future in futures:
                    future.result()
            except BaseException:  # a failed square (or Ctrl-C): don't start the rest; finished ones are saved
                pool.shutdown(wait=False, cancel_futures=True)
                raise
    return [row for sq in squares for row in rows(sq)]


def inside(bbox: Bbox, lon: float, lat: float) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


# ── areas saved before the grid ──────────────────────────────────────────────────────────────────
# These lookups used to be saved per poster area, with cache/areas.json listing the finished areas.
# An old area that covers the poster is still used, so trips made before the grid don't download again.

def old_area_rows(kind: str, bbox: Bbox, key_of):
    """What was saved for an old area of this `kind` that covers `bbox` (its key: `key_of(area)`), or None."""
    s, w, n, e = bbox
    tol = 1e-6  # degrees (~0.1 m): two sizes fitted to the same route share edges up to rounding
    for area in _areas().get(kind, []):
        S, W, N, E = area
        if S <= s + tol and W <= w + tol and N >= n - tol and E >= e - tol and is_cached(kind, key_of(area)):
            return json.loads(cache_path(kind, key_of(area)).read_text())
    return None


def _areas() -> dict[str, list]:
    path = cache_dir() / "areas.json"
    return json.loads(path.read_text()) if path.exists() else {}
