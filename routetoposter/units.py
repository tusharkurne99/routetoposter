"""Distances and altitudes in the trip's units: metric (km, m) or imperial (miles, feet).

Everything inside works in km and metres; only the text on the poster and in the terminal changes.
"""
KM_PER_MILE = 1.609344
FT_PER_M = 3.28084
# Scale bar lengths to choose from, in the bar's own unit (km or miles).
NICE_LENGTHS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]


def distance(km: float, units: str) -> str:
    """'1,188 KM' or '738 MI'."""
    if units == "imperial":
        return f"{km / KM_PER_MILE:,.0f} MI"
    return f"{km:,.0f} KM"


def height(m: float, units: str) -> str:
    """'4,551 M' or '14,931 FT'."""
    if units == "imperial":
        return f"{m * FT_PER_M:,.0f} FT"
    return f"{m:,.0f} M"


def scale_length(km_per_inch: float, max_inches: float, units: str) -> tuple[float, int, str]:
    """The longest round scale bar that fits in `max_inches`: (its length in inches, the number on it,
    'KM' or 'MI')."""
    unit_km, word = (KM_PER_MILE, "MI") if units == "imperial" else (1.0, "KM")
    n = max((n for n in NICE_LENGTHS if n * unit_km / km_per_inch <= max_inches), default=NICE_LENGTHS[0])
    return n * unit_km / km_per_inch, n, word
