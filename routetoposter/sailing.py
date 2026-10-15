"""Sea crossings that go round the land.

A sailing leg is planned on a grid over the area around it: each cell is water or land (from the
coastline, mapdata.fetch_sea), the shortest way through water cells that keep a margin from the
shore (CLEARANCE_SHARE of the leg) is found (A*), cut down to the few points where it turns (any two points with open water
between them are joined directly), and drawn as straight runs with each turn rounded into a curve
(a quadratic Bézier, so a parabola). Every curve is checked against the water; one that would clip
land is rounded less. With open water all the way it's a gentle arc, like a flight but flatter.

The search area is the leg's box widened by MARGIN of its length; if the land can't be passed
inside it (a long peninsula), it is widened up to twice more. Ports usually lie a little inland on
the map, so the line starts and ends at the stops and joins the water at the nearest open cell.
"""
import heapq
import math

import numpy as np
import shapely
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .trip import Stop

Point = tuple[float, float]  # (lon, lat)

GRID_CELLS = 300  # cells along the longer side of the search area
MARGIN = 0.5  # the search area: the leg's box widened by this share of the leg's length on every side…
MIN_MARGIN_KM = 15  # …but at least this (a short hop round a headland)
GROW = (1, 2, 4)  # the margin is multiplied by these in turn until a way round is found
MAX_LEG_KM = 1500  # longer crossings would need the coastline of half an ocean; they are drawn as arcs
# Keep this share of the leg's length off the shore (at least CLEARANCE_CELLS), so the line doesn't
# run along the coast. Where that closes a narrow channel, it is tried again with CLEARANCE_CELLS.
CLEARANCE_SHARE = 0.03
CLEARANCE_CELLS = 2
SEA_BEND = 0.06  # open water: the arc bows out this share of its length (flights: route.ARC_BEND)
CORNER = 0.45  # a turn is rounded over this share of the runs on either side, less if that clips land
CURVE_POINTS = 16
KM_PER_DEG = 111.32


def sea_route(a: Stop, b: Stop, sea_for) -> list[Point] | None:
    """The line from `a` to `b` through water, or None when no way round the land was found (or the
    coastline isn't known there). `sea_for(bbox)` returns the sea polygons for (south, west, north,
    east), as mapdata.fetch_sea does."""
    km = math.hypot((b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2)), b.lat - a.lat) * KM_PER_DEG
    if km > MAX_LEG_KM:
        return None
    for grow in GROW:
        bbox = _search_box(a, b, km, grow)
        sea = sea_for(bbox)
        if not sea:
            return None  # no coastline here: can't tell sea from land
        water = unary_union([Polygon(rings[0], rings[1:]) for rings in sea]).buffer(0)
        water = water.intersection(box(bbox[1], bbox[0], bbox[3], bbox[2]))
        for clearance in (CLEARANCE_SHARE * km, 0):
            path = _plan(a, b, water, bbox, clearance)
            if path:
                return path
    return None


def _search_box(a: Stop, b: Stop, km: float, grow: float) -> tuple[float, float, float, float]:
    pad = max(MARGIN * km, MIN_MARGIN_KM) * grow / KM_PER_DEG
    c = math.cos(math.radians((a.lat + b.lat) / 2))
    return (min(a.lat, b.lat) - pad, min(a.lon, b.lon) - pad / c, max(a.lat, b.lat) + pad, max(a.lon, b.lon) + pad / c)


class _Grid:
    """The search area as cells, in a local projection (x = lon · cos(lat), y = lat) so cells are square."""

    def __init__(self, water, bbox, clearance_km: float):
        s, w, n, e = bbox
        self.c = math.cos(math.radians((s + n) / 2))
        self.x0, self.y0 = w * self.c, s
        self.cell = max((e - w) * self.c, n - s) / GRID_CELLS
        self.nx = max(2, math.ceil((e - w) * self.c / self.cell))
        self.ny = max(2, math.ceil((n - s) / self.cell))
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.cell
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.cell
        gx, gy = np.meshgrid(xs, ys)
        shapely.prepare(water)
        self.water = shapely.contains_xy(water, gx / self.c, gy)  # [row (y), column (x)]
        self.margin = max(CLEARANCE_CELLS, round(clearance_km / KM_PER_DEG / self.cell))
        near_land = ~self.water
        for _ in range(self.margin):  # grow the land by one cell in all 8 directions
            grown = near_land.copy()
            grown[1:, :] |= near_land[:-1, :]
            grown[:-1, :] |= near_land[1:, :]
            grown[:, 1:] |= near_land[:, :-1]
            grown[:, :-1] |= near_land[:, 1:]
            grown[1:, 1:] |= near_land[:-1, :-1]
            grown[:-1, :-1] |= near_land[1:, 1:]
            grown[1:, :-1] |= near_land[:-1, 1:]
            grown[:-1, 1:] |= near_land[1:, :-1]
            near_land = grown
        self.open = self.water & ~near_land  # water with room round it

    def xy(self, lon: float, lat: float) -> np.ndarray:
        return np.array([lon * self.c, lat])

    def lonlat(self, p) -> Point:
        return float(p[0] / self.c), float(p[1])

    def centre(self, cell: tuple[int, int]) -> np.ndarray:
        row, col = cell
        return np.array([self.x0 + (col + 0.5) * self.cell, self.y0 + (row + 0.5) * self.cell])

    def nearest_open(self, p: np.ndarray) -> tuple[int, int] | None:
        rows, cols = np.nonzero(self.open)
        if not len(rows):
            return None
        d = np.hypot(self.x0 + (cols + 0.5) * self.cell - p[0], self.y0 + (rows + 0.5) * self.cell - p[1])
        i = int(np.argmin(d))
        return int(rows[i]), int(cols[i])

    def clear(self, points: np.ndarray, mask: np.ndarray) -> bool:
        """Are all `points` (x, y) on cells that are True in `mask`?"""
        cols = np.floor((points[:, 0] - self.x0) / self.cell).astype(int)
        rows = np.floor((points[:, 1] - self.y0) / self.cell).astype(int)
        inside = (cols >= 0) & (cols < self.nx) & (rows >= 0) & (rows < self.ny)
        return bool(inside.all() and mask[rows, cols].all())

    def sight(self, p: np.ndarray, q: np.ndarray, mask: np.ndarray) -> bool:
        """Open water (by `mask`) all along the straight line from p to q?"""
        n = max(2, int(np.hypot(*(q - p)) / (self.cell / 2)) + 1)
        return self.clear(p + np.linspace(0, 1, n)[:, None] * (q - p), mask)


def _plan(a: Stop, b: Stop, water, bbox, clearance_km: float) -> list[Point] | None:
    grid = _Grid(water, bbox, clearance_km)
    pa, pb = grid.xy(a.lon, a.lat), grid.xy(b.lon, b.lat)
    start, goal = grid.nearest_open(pa), grid.nearest_open(pb)
    if start is None or goal is None:
        return None
    cells = _astar(grid.open, start, goal)
    if cells is None:
        return None
    turns = _pull([grid.centre(cell) for cell in cells], grid)
    line = _rounded([pa] + turns + [pb], grid)
    return [grid.lonlat(p) for p in line]


def _astar(open_: np.ndarray, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
    """Shortest 8-connected path over True cells, or None."""
    steps = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    ny, nx = open_.shape
    best = {start: 0.0}
    came: dict = {}
    queue = [(0.0, start)]
    while queue:
        _, cell = heapq.heappop(queue)
        if cell == goal:
            path = [cell]
            while cell in came:
                cell = came[cell]
                path.append(cell)
            return path[::-1]
        r, c = cell
        for dr, dc in steps:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < ny and 0 <= nc < nx and open_[nr, nc]):
                continue
            cost = best[cell] + (1.4142 if dr and dc else 1.0)
            if cost < best.get((nr, nc), math.inf):
                best[(nr, nc)] = cost
                came[(nr, nc)] = cell
                heapq.heappush(queue, (cost + math.hypot(goal[0] - nr, goal[1] - nc), (nr, nc)))
    return None


def _pull(points: list[np.ndarray], grid: _Grid) -> list[np.ndarray]:
    """Only the turns: from each kept point, jump to the furthest later point in open water's sight."""
    kept, i = [points[0]], 0
    while i < len(points) - 1:
        j = len(points) - 1
        while j > i + 1 and not grid.sight(points[i], points[j], grid.open):
            j -= 1
        kept.append(points[j])
        i = j
    return kept


def _rounded(points: list[np.ndarray], grid: _Grid) -> list[np.ndarray]:
    """Straight runs with every turn rounded off by a parabola, each checked to stay on water. The
    first and last runs (from the ports, which may lie inland) aren't checked. Two points only:
    a gentle arc, else a straight line."""
    if len(points) == 2:
        p, q = points
        for side in (1, -1):  # bow left of the way sailed, as flights do; else right
            arc = _bezier(p, (p + q) / 2 + side * 2 * SEA_BEND * np.array([p[1] - q[1], q[0] - p[0]]), q, 2 * CURVE_POINTS)
            if grid.clear(_off_port(arc, p, q, grid), grid.water):
                return list(arc)
        return [p, q]
    out = [points[0]]
    for k in range(1, len(points) - 1):
        prev, turn, nxt = points[k - 1], points[k], points[k + 1]
        for share in (CORNER, CORNER / 2, CORNER / 4):
            a = turn + (prev - turn) * share
            b = turn + (nxt - turn) * share
            curve = _bezier(a, turn, b, CURVE_POINTS)
            if grid.clear(_off_port(curve, points[0], points[-1], grid), grid.water):
                out.extend(curve)
                break
        else:
            out.append(turn)
    out.append(points[-1])
    return out


def _off_port(line: np.ndarray, p: np.ndarray, q: np.ndarray, grid: _Grid) -> np.ndarray:
    """The points of `line` not right next to either port (ports are often a little inland)."""
    reach = (grid.margin + 2) * grid.cell
    far = (np.hypot(*(line - p).T) > reach) & (np.hypot(*(line - q).T) > reach)
    return line[far] if far.any() else line[:0]


def _bezier(p: np.ndarray, control: np.ndarray, q: np.ndarray, n: int) -> np.ndarray:
    t = np.linspace(0, 1, n)[:, None]
    return (1 - t) ** 2 * p + 2 * t * (1 - t) * control + t ** 2 * q
