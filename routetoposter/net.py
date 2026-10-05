"""HTTP requests with retries, and an on-disk JSON cache so nothing is downloaded twice."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "routetoposter/0.2 (road-trip poster generator)"
RETRY_STATUS = {429, 502, 503, 504}  # "busy" answers worth retrying


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
            last_error = e
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
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


def is_cached(kind: str, key) -> bool:
    return cache_path(kind, key).exists()


def cached_json(kind: str, key, produce):
    """The cached value for `key`; on a miss, call `produce()`, save what it returns, and return it."""
    path = cache_path(kind, key)
    if path.exists():
        return json.loads(path.read_text())
    value = produce()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


# ── saved areas ──────────────────────────────────────────────────────────────────────────────────
# Map data is cached per area (south, west, north, east). Another poster size or orientation needs a
# slightly different area; if a bigger saved area covers it, that one is reused instead of
# downloading again (what lies outside the page is simply not visible). cache/areas.json lists the
# areas whose download finished, per kind of data.

def saved_area(kind: str, bbox) -> tuple[float, float, float, float]:
    """A finished area of this `kind` that covers `bbox`, or `bbox` itself if there is none."""
    s, w, n, e = bbox
    tol = 1e-6  # degrees (~0.1 m): two sizes fitted to the same route share edges up to rounding
    for S, W, N, E in _areas().get(kind, []):
        if S <= s + tol and W <= w + tol and N >= n - tol and E >= e - tol:
            return S, W, N, E
    return tuple(float(v) for v in bbox)


def remember_area(kind: str, bbox) -> None:
    """Note that all data of this `kind` for `bbox` is now cached."""
    areas = _areas()
    entry = [float(v) for v in bbox]
    if entry not in areas.setdefault(kind, []):
        areas[kind].append(entry)
        path = cache_dir() / "areas.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(areas, indent=1))


def _areas() -> dict[str, list]:
    path = cache_dir() / "areas.json"
    return json.loads(path.read_text()) if path.exists() else {}
