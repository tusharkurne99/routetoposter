"""Poster size, and how the map is placed on it.

Everything on the poster is measured in inches with the origin at the bottom-left corner. A Frame
turns (lon, lat) into poster inches with a simple local projection: longitude is shrunk by
cos(latitude) so that a kilometre is the same length in both directions.
"""
import math
from dataclasses import dataclass

import numpy as np

from .trip import SIZES, Trip

# Where the route must fit, as fractions of the poster (x0, y0, x1, y1). The bottom ~30% is left
# for the title block, as in maptoposter.
ROUTE_BOX = (0.07, 0.30, 0.93, 0.92)
ROUTE_PAD = 0.04  # extra margin inside ROUTE_BOX, as a fraction of its size
KM_PER_DEG = 111.32  # km per degree of latitude
PIXEL_SIZE_DPI = 300  # pixel sizes (instagram…) are drawn at this dpi
PREVIEW_LONG_EDGE_PX = 1200


@dataclass
class Frame:
    width: float  # poster size, inches
    height: float
    lat0: float  # latitude the projection is centred on
    k: float  # inches per degree of latitude
    ox: float  # offsets, inches
    oy: float

    @property
    def c(self) -> float:
        """cos(lat0): how much a degree of longitude is shorter than a degree of latitude."""
        return math.cos(math.radians(self.lat0))

    def project(self, lon, lat):
        """(lon, lat) -> (x, y) inches. Works on single numbers and numpy arrays."""
        lon, lat = np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
        return self.k * lon * self.c + self.ox, self.k * lat + self.oy

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        """(x, y) inches -> (lon, lat)."""
        return (x - self.ox) / (self.k * self.c), (y - self.oy) / self.k

    def bbox(self, margin: float = 0.02) -> tuple[float, float, float, float]:
        """(south, west, north, east) of the whole poster plus `margin` (fraction of its size) on
        every side: the area whose map data is needed."""
        mx, my = self.width * margin, self.height * margin
        west, south = self.unproject(-mx, -my)
        east, north = self.unproject(self.width + mx, self.height + my)
        return south, west, north, east

    def area_km2(self) -> float:
        """Ground area the poster covers."""
        s, w, n, e = self.bbox(0)
        return (n - s) * KM_PER_DEG * (e - w) * KM_PER_DEG * self.c

    def km_per_inch(self) -> float:
        return KM_PER_DEG / self.k


def poster_inches(trip: Trip) -> tuple[float, float]:
    """(width, height) in inches for the trip's size and orientation."""
    w, h, unit = SIZES[trip.size]
    per_inch = {"in": 1, "mm": 25.4, "px": PIXEL_SIZE_DPI}[unit]
    w, h = w / per_inch, h / per_inch
    return (h, w) if trip.orientation == "landscape" else (w, h)


def output_dpi(trip: Trip, width: float, height: float, preview: bool) -> float:
    """Dots per inch to save at: a preview is ~1200 px on its long edge; pixel sizes are exact."""
    if preview:
        return PREVIEW_LONG_EDGE_PX / max(width, height)
    return PIXEL_SIZE_DPI if SIZES[trip.size][2] == "px" else trip.dpi


def fit(coords: list[tuple[float, float]], width: float, height: float) -> Frame:
    """The Frame that fits the route (`coords`, (lon, lat)) as large as possible into ROUTE_BOX,
    centred, keeping its true shape."""
    lons = np.array([c[0] for c in coords])
    lats = np.array([c[1] for c in coords])
    lat0 = float((lats.min() + lats.max()) / 2)
    c = math.cos(math.radians(lat0))
    px, py = lons * c, lats
    span_x = max(px.max() - px.min(), 0.01)
    span_y = max(py.max() - py.min(), 0.01)

    bx0, by0 = ROUTE_BOX[0] * width, ROUTE_BOX[1] * height
    bx1, by1 = ROUTE_BOX[2] * width, ROUTE_BOX[3] * height
    k = min((bx1 - bx0) / span_x, (by1 - by0) / span_y) * (1 - 2 * ROUTE_PAD)
    ox = (bx0 + bx1) / 2 - k * (px.min() + px.max()) / 2
    oy = (by0 + by1) / 2 - k * (py.min() + py.max()) / 2
    return Frame(width, height, lat0, k, ox, oy)
