"""Route helpers, layout, map detail, the sea, poster text, and an offline render. All offline."""
import re

import pytest
from PIL import Image

from routetoposter.build import stats_line, subtitle, trip_days
from routetoposter.extras import Pass, Sight
from routetoposter.layout import fit, output_dpi, poster_inches
from routetoposter.pelennor_fields import BorderText
from routetoposter.mapdata import download_level, fetch_sea, roads_to_draw, sea_polygons
from routetoposter.render import Poster, PosterText, render
from routetoposter.route import Leg, Route, decode_polyline6, describe, route_trip
from routetoposter.style import load_fonts, load_theme
from routetoposter.trip import Stop, Trip

STOPS = [Stop("Manali", 32.24, 77.19, nights=1), Stop("Kaza", 32.23, 78.07, nights=2), Stop("Manali", 32.24, 77.19)]


def test_straight_route_and_its_description():
    route = route_trip(STOPS, "straight")
    assert len(route.legs) == 2 and 75 < route.legs[0].km < 90
    lines = describe(route, STOPS)
    assert "straight line" in lines[0]
    assert lines[-1].startswith("Total") and "loop (starts and ends in Manali)" in lines[-1]


def test_decode_polyline6():
    assert decode_polyline6("_izlhA~rlgdF_{geC~ywl@") == [(-120.2, 38.5), (-120.95, 40.7)]


def test_sizes_orientation_and_dpi():
    assert poster_inches(Trip(stops=STOPS, size="12x16")) == (12, 16)
    assert poster_inches(Trip(stops=STOPS, size="12x16", orientation="landscape")) == (16, 12)
    w, h = poster_inches(Trip(stops=STOPS, size="instagram"))
    assert round(w * 300) == 1080 and round(h * 300) == 1350
    assert output_dpi(Trip(stops=STOPS), 12, 16, preview=True) == 75


def test_vistaprint_sizes_include_the_bleed():
    from routetoposter.layout import bleed_inches

    trip = Trip(stops=STOPS, size="VP-18x24", orientation="landscape", dpi=300)
    assert poster_inches(trip) == (24.5, 18.5) and bleed_inches(trip) == 0.25
    assert bleed_inches(Trip(stops=STOPS, size="18x24")) == 0


def test_route_fits_inside_the_map_area():
    frame = fit([(77.0, 32.0), (78.0, 32.5)], 12, 16)
    x, y = frame.project([77.0, 78.0], [32.0, 32.5])
    assert 0.07 * 12 <= min(x) and max(x) <= 0.93 * 12
    assert 0.30 * 16 <= min(y) and max(y) <= 0.92 * 16


def test_download_level_by_area():
    assert download_level(10_000, "auto") == "high"
    assert download_level(400_000, "auto") == "minimal"
    assert download_level(400_000, "more") == "low"


def test_dense_roads_are_dropped_but_main_roads_stay():
    line = [[0, 0], [1, 0]]  # 1 degree ≈ 111 km
    features = {"motorway": [line], "primary": [line], "secondary": [line] * 1000, "tertiary": [line] * 1000}  # covers ~34%
    roads = roads_to_draw(features, km_per_inch=50, map_area_in2=100, scale=1, map_detail="auto")
    assert roads == ["motorway", "primary"]
    assert roads_to_draw({"motorway": [line], "secondary": [line]}, 50, 100, 1, "auto") == \
        ["motorway", "primary", "secondary", "tertiary", "residential", "default"]


def test_sea_is_on_the_right_of_the_coastline():
    # Coastline running north → south at lon 0.5: land on its left (east), sea on its right (west).
    [sea] = sea_polygons([[[0.5, 2], [0.5, -1]]], (0, 0, 1, 1))
    xs = [p[0] for p in sea[0]]
    assert min(xs) == 0 and max(xs) == 0.5


def test_island_is_a_hole_in_the_sea():
    island = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]  # anticlockwise: land inside
    [sea] = sea_polygons([island], (0, 0, 1, 1))
    assert len(sea) == 2  # the outline and one hole


def test_no_coast_no_sea():
    assert sea_polygons([], (0, 0, 1, 1)) == []


def test_subtitle_days_and_stats():
    trip = Trip(stops=STOPS)
    assert subtitle(trip) == "Manali · Kaza"
    assert trip_days(trip) == 4
    route = Route([], [Leg("A", "B", 833.4, 10)])
    passes = [Pass("Kunzum La", 0, 0, 4551), Pass("Highest point", 0, 0, 4600)]
    assert stats_line(trip, route, passes, 4600) == "833 KM  ·  4 DAYS  ·  1 PASS  ·  HIGHEST 4,600 M"
    assert subtitle(Trip(stops=STOPS, subtitle=None)) == ""
    imperial = Trip(stops=STOPS, units="imperial")
    assert stats_line(imperial, route, passes, 4600) == "518 MI  ·  4 DAYS  ·  1 PASS  ·  HIGHEST 15,092 FT"


def test_a_theme_can_colour_each_kind_of_leg():
    galapagos, plain = load_theme("galapagos"), load_theme("terracotta")
    assert galapagos["route_sailing"] != galapagos["route"] == galapagos["route_driving"]
    assert plain["route_sailing"] == plain["route_flying"] == plain["route"]


def test_scale_bar_is_a_round_number_of_km_or_miles():
    from routetoposter.units import KM_PER_MILE, scale_length

    assert scale_length(30, 2.0, "metric") == (50 / 30, 50, "KM")
    length, n, word = scale_length(30, 2.0, "imperial")
    assert (n, word) == (25, "MI") and length == pytest.approx(25 * KM_PER_MILE / 30)


def test_render_offline(tmp_path):
    """A whole poster from made-up map data: every layer, no network."""
    route = Route([(77.19, 32.24), (77.6, 32.0), (78.07, 32.23), (77.5, 32.4), (77.19, 32.24)],
                  [Leg("Manali", "Kaza", 200, 5), Leg("Kaza", "Manali", 200, 5)])
    frame = fit(route.coords, 4, 5)
    features = {"primary": [[[77.0, 32.0], [78.2, 32.3]]], "river": [[[77.0, 32.1], [78.2, 32.1]]],
                "lake": [[[77.7, 32.3], [77.8, 32.3], [77.8, 32.35], [77.7, 32.3]]],
                "named_rivers": [["Spiti River", [[77.0, 32.1], [78.2, 32.1]]]], "named_lakes": []}
    sea = sea_polygons([[[76.95, 33.0], [76.95, 31.0]]], frame.bbox())
    poster = Poster(frame=frame, features=features, roads=["primary"], sea=sea, route=route, stops=STOPS,
                    stop_heights=[2050, 3650, 2050], passes=[Pass("Kunzum La", 77.6, 32.0, 4551)],
                    sights=[Sight("Key Monastery", 78.01, 32.29, "monastery", stop=(78.07, 32.23))],
                    text=PosterText("Spiti Valley", "Manali · Kaza", "400 KM", "JUNE 2026"))
    out = tmp_path / "poster.png"
    render(str(out), poster, load_theme("terracotta"), load_fonts(), dpi=50)
    assert Image.open(out).size == (200, 250)


def test_failed_optional_lookup_does_not_stop_the_poster(capsys):
    from routetoposter.build import optional

    def broken():
        raise OSError("Network is unreachable")

    assert optional("passes", broken, ([], [])) == ([], [])
    assert "Couldn't look up passes (Network is unreachable)" in capsys.readouterr().out


def test_unreachable_server_skips_a_sight_but_a_wrong_name_stops(monkeypatch, capsys):
    import pytest

    from routetoposter import sights
    from routetoposter.errors import UserError
    from routetoposter.trip import NearbySight

    stop = Stop("Kochi", 9.93, 76.26, sights_nearby=[NearbySight("Fort Kochi")])

    def server_down(*args, **kwargs):
        raise UserError("The OpenStreetMap servers are busy or unreachable")

    monkeypatch.setattr(sights, "search", server_down)
    assert sights.locate_sights([stop]) == []
    assert "Couldn't look up 'Fort Kochi'" in capsys.readouterr().out

    monkeypatch.setattr(sights, "search", lambda *args: [])  # the server answered: no such place
    monkeypatch.setattr(sights, "suggest", lambda *args: [])  # and nothing similar nearby
    with pytest.raises(UserError, match="'Fort Kochi' near Kochi"):
        sights.locate_sights([stop])


def _old_area(tmp_path, kind, area):
    import json
    (tmp_path / "areas.json").write_text(json.dumps({kind: [list(area)]}))


def test_an_area_saved_before_the_grid_is_still_used(tmp_path, monkeypatch):
    from routetoposter import extras
    from routetoposter.net import cache_path

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    old = (30.0, 76.0, 33.0, 79.0)
    _old_area(tmp_path, "passes", old)
    path = cache_path("passes", [[round(v, 4) for v in old], extras.POI_VERSION])
    path.parent.mkdir(parents=True)
    path.write_text('[["Kunzum La", 77.64, 32.4, "4551"], ["Far Away La", 78.9, 30.1, null]]')
    monkeypatch.setattr(extras, "overpass", lambda *a, **k: pytest.fail("downloaded again"))
    assert [p.name for p in extras.fetch_passes((32.0, 77.0, 33.0, 78.0))] == ["Kunzum La"]  # covered; only what's inside


def test_an_old_area_that_doesnt_cover_the_poster_is_not_used(tmp_path, monkeypatch):
    from routetoposter.net import old_area_rows

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    _old_area(tmp_path, "coastline", (30.5, 76.86575341102125, 32.8, 78.88426958897878))
    assert old_area_rows("coastline", (29.0, 77.0, 32.0, 78.0), lambda a: a) is None  # sticks out
    assert old_area_rows("coastline", (30.6, 76.86575341102120, 32.7, 78.8842695889788), lambda a: a) is None  # covered, nothing saved


def test_a_shifted_area_only_asks_for_the_squares_it_adds(tmp_path, monkeypatch):
    from routetoposter import extras

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    asked = []

    def overpass(query, patient=True, notice=None):
        asked.append(re.search(r"\(([-\d.,]+)\)", query).group(1))
        return {"elements": [{"lat": 32.5, "lon": 77.5, "tags": {"name": "Rohtang"}, "type": "node"}]}

    monkeypatch.setattr(extras, "overpass", overpass)
    extras.fetch_sight_candidates((32.2, 77.2, 32.8, 77.8))
    assert asked == ["32.0000,77.0000,33.0000,78.0000"]
    [sight] = extras.fetch_sight_candidates((32.3, 77.3, 32.9, 78.6))  # moved and wider: one new square
    assert asked[1:] == ["32.0000,78.0000,33.0000,79.0000"]
    assert sight.name == "Rohtang"  # the same place from two squares, once


def test_coastline_ways_from_two_squares_count_once(tmp_path, monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    # One coastline way north → south across two squares: Overpass returns it with each square.
    way = {"type": "way", "geometry": [{"lon": 1.0, "lat": 3.0}, {"lon": 1.0, "lat": -1.0}]}
    monkeypatch.setattr(mapdata, "overpass", lambda query, patient=True, notice=None: {"elements": [way]})
    monkeypatch.setattr(mapdata, "COAST_GRID_DEG", 1.0)
    bbox = (0.2, 0.5, 1.8, 1.5)
    assert fetch_sea(bbox) == sea_polygons([[[1.0, 3.0], [1.0, -1.0]]], bbox)


def test_a_huge_detour_is_flagged():
    stops = [Stop("Pangong Tso", 33.7595, 78.6674), Stop("Rezang La", 33.4182, 78.8475)]
    lines = describe(Route([], [Leg("Pangong Tso", "Rezang La", 495, 10)]), stops)
    assert "⚠ see below" in lines[0]
    assert any("only 41 km apart" in line for line in lines)
    normal = describe(Route([], [Leg("Pangong Tso", "Rezang La", 90, 2)]), stops)
    assert not any("⚠" in line for line in normal)


def test_passes_are_read_with_their_positions(tmp_path, monkeypatch):
    from routetoposter import extras

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    answer = {"elements": [{"type": "node", "id": 1, "lat": 34.2787, "lon": 77.6047,
                            "tags": {"mountain_pass": "yes", "name": "Khardung La", "ele": "5359"}}]}
    queries = []
    monkeypatch.setattr(extras, "overpass", lambda query, patient=True, notice=None, light=False: queries.append(query) or answer)
    [khardung] = extras.fetch_passes((34.0, 77.0, 35.0, 78.0))
    assert (khardung.name, khardung.lon, khardung.lat, khardung.ele) == ("Khardung La", 77.6047, 34.2787, 5359)
    assert queries[0].endswith("out;")  # `out tags;` would leave out the positions


def test_grid_squares_are_fixed_and_shared():
    from routetoposter.mapdata import grid_squares

    assert grid_squares((30.6, 76.9, 31.4, 77.4), "high") == [(30.5, 76.5, 31.0, 77.0), (30.5, 77.0, 31.0, 77.5),
                                                              (31.0, 76.5, 31.5, 77.0), (31.0, 77.0, 31.5, 77.5)]


def test_another_size_only_downloads_the_new_squares(tmp_path, monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    road = {"type": "way", "tags": {"highway": "primary"},
            "geometry": [{"lon": 76.9, "lat": 31.2}, {"lon": 77.1, "lat": 31.2}]}  # crosses two squares
    asked = []
    monkeypatch.setattr(mapdata, "overpass", lambda query, patient=True, notice=None: asked.append(query) or {"elements": [road]})

    first = mapdata.fetch_features((31.1, 76.8, 31.4, 77.3), "high")
    assert len(asked) == 2 and len(first["primary"]) == 1  # two squares; the road kept once
    mapdata.fetch_features((31.15, 76.85, 31.35, 77.25), "high")  # a smaller poster: all saved
    assert len(asked) == 2
    mapdata.fetch_features((31.1, 76.8, 31.4, 77.8), "high")  # wider: one more square
    assert len(asked) == 3


def _age(path, days):
    import os, time
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_saved_downloads_are_kept_until_cache_days(monkeypatch):
    from routetoposter.net import cache_path, cached_json

    asked = []
    cached_json("t", "k", lambda: asked.append(1) or "first")
    _age(cache_path("t", "k"), 200)
    assert cached_json("t", "k", lambda: pytest.fail("no limit set: kept forever")) == "first"
    monkeypatch.setenv("ROUTETOPOSTER_CACHE_DAYS", "365")
    assert cached_json("t", "k", lambda: pytest.fail("200 days < 365")) == "first"
    monkeypatch.setenv("ROUTETOPOSTER_CACHE_DAYS", "120")
    assert cached_json("t", "k", lambda: "second") == "second"  # too old: downloaded again
    assert cached_json("t", "k", lambda: pytest.fail("just saved")) == "second"
    monkeypatch.setenv("ROUTETOPOSTER_CACHE_DAYS", "0")
    _age(cache_path("t", "k"), 5000)
    assert cached_json("t", "k", lambda: pytest.fail("0 = forever")) == "second"


def test_an_old_copy_is_used_when_the_refresh_fails(monkeypatch):
    from routetoposter import mapdata
    from routetoposter.net import cache_path, cached_json

    def down():
        raise OSError("offline")

    cached_json("t", "k", lambda: "saved")
    _age(cache_path("t", "k"), 200)
    monkeypatch.setenv("ROUTETOPOSTER_CACHE_DAYS", "120")
    assert cached_json("t", "k", down) == "saved"

    road = {"type": "way", "tags": {"highway": "primary"}, "geometry": [{"lon": 77.1, "lat": 31.2}, {"lon": 77.2, "lat": 31.2}]}
    monkeypatch.setattr(mapdata, "overpass", lambda *a, **k: {"elements": [road]})
    bbox = (31.1, 77.05, 31.4, 77.3)
    mapdata.fetch_features(bbox, "high")
    for path in (cache_path("osm-grid", mapdata._square_key(sq, "high")) for sq in mapdata.grid_squares(bbox, "high")):
        _age(path, 200)
    assert mapdata.tiles_status(bbox, "high") == (0, 1, 0)  # counted as to download
    monkeypatch.setattr(mapdata, "overpass", lambda *a, **k: down())
    assert len(mapdata.fetch_features(bbox, "high")["primary"]) == 1


def test_bad_cache_days_is_a_user_error(monkeypatch):
    from routetoposter.errors import UserError
    from routetoposter.net import cache_max_days

    monkeypatch.setenv("ROUTETOPOSTER_CACHE_DAYS", "a year")
    with pytest.raises(UserError):
        cache_max_days()


def test_squares_that_are_all_sea_are_not_downloaded(tmp_path, monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    asked = []
    monkeypatch.setattr(mapdata, "overpass", lambda query, patient=True, notice=None: asked.append(query) or {"elements": []})
    bbox = (0.1, 0.1, 0.4, 1.9)  # one row of four 0.5° squares
    sea = sea_polygons([[[0.7, 0.0], [0.7, 0.15], [0.7, 0.25], [0.7, 0.35], [0.7, 0.5]]], bbox)  # a coast running north: sea on its right, east of 0.7
    assert mapdata.tiles_status(bbox, "high", sea) == (0, 2, 2)  # the two squares east of lon 1.0 are sea
    mapdata.fetch_features(bbox, "high", sea=sea)
    assert sorted(re.search(r"\(([\d.,]+)\)", q).group(1) for q in asked) == ["0.0000,0.0000,0.5000,0.5000",
                                                                            "0.0000,0.5000,0.5000,1.0000"]
    island_coast = [[1.5, 0.2], [1.6, 0.2], [1.6, 0.3], [1.5, 0.3], [1.5, 0.2]]  # land on the left: anticlockwise
    island = sea_polygons([[[0.7, 0.0], [0.7, 0.15], [0.7, 0.25], [0.7, 0.35], [0.7, 0.5]], island_coast], bbox)
    assert mapdata.tiles_status(bbox, "high", island)[1:] == (3, 1)  # the island's square is kept
    assert mapdata.tiles_status(bbox, "high", [])[1:] == (4, 0)  # no coastline: nothing skipped


def test_squares_download_two_at_a_time_and_progress_counts_downloads(tmp_path, monkeypatch):
    import threading
    import time
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    running, most = [0], [0]
    lock = threading.Lock()

    def slow(query, patient=True, notice=None):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return {"elements": []}

    monkeypatch.setattr(mapdata, "overpass", slow)
    progress = []
    mapdata.fetch_features((0.1, 0.1, 0.4, 1.9), "high", on_tile=lambda i, n: progress.append((i, n)))
    assert most[0] == mapdata.OVERPASS_SLOTS
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_a_partly_saved_square_is_used_only_for_its_saved_part(tmp_path, monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    square = (31.0, 77.0, 31.5, 77.5)
    mapdata._save_square(square, "high", (31.0, 77.0, 31.5, 77.2), {"primary": []})  # only the west part saved
    assert mapdata.tiles_status((31.1, 77.05, 31.4, 77.15), "high") == (1, 1, 0)
    assert mapdata.tiles_status((31.1, 77.05, 31.4, 77.40), "high") == (0, 1, 0)


def test_a_pass_named_only_mountain_pass_is_left_out():
    from routetoposter.extras import passes_on_route

    route = [[(77.0, 34.0), (78.0, 34.0)]]
    passes = [Pass("Mountain pass", 77.5, 34.0, None), Pass("Chang La", 77.6, 34.0, 5360)]
    assert [p.name for p in passes_on_route(passes, route)] == ["Chang La"]


def test_runes_and_numbers_in_words():
    from routetoposter.runes import number_words, to_runes

    assert to_runes("Spiti Valley") == "ᛋᛈᛁᛏᛁ ᚠᚪᛚᛚᛖᚣ"
    assert to_runes("There and back again") == "ᚦᛖᚱᛖ ᚪᚾᛞ ᛒᚪᚳᚳ ᚪᚷᚪᛁᚾ"  # TH is one rune
    assert to_runes("Kaza 3670") == "ᚳᚪᛋᚪ "  # runes have no digits
    assert [number_words(n) for n in (3, 10, 42, 833)] == ["three", "ten", "forty-two", "eight hundred thirty-three"]


def test_border_text_lists_stops_once_and_facts_in_words():
    from routetoposter.build import border_text

    trip = Trip(stops=STOPS, runes_left="Our own words")
    border = border_text(trip, [Pass("Kunzum La", 0, 0, 4551), Pass("Highest point", 0, 0, 4600)])
    assert border.top == "Manali · Kaza"  # Manali again at the end isn't repeated
    assert border.bottom == "four days · one pass · over Kunzum La"
    assert (border.left, border.right) == ("Our own words", None)  # None = the theme's line


def test_every_font_is_its_own_file_with_a_fallback():
    from matplotlib.font_manager import FontProperties, findfont

    from routetoposter.style import DEFAULT_FONTS, FALLBACK_FONT, FONT_ROOT

    theme = load_theme("pelennor_fields")
    for files, fonts in ((DEFAULT_FONTS, load_fonts()), ({**DEFAULT_FONTS, **theme["fonts"]}, load_fonts(theme))):
        for role, file in files.items():
            fp = fonts[role]
            assert fp.get_family()[1] == FALLBACK_FONT
            own = FontProperties(family=fp.get_family()[0], weight=fp.get_weight(), style=fp.get_style())
            assert findfont(own, fallback_to_default=False) == str(FONT_ROOT / file), role


def test_an_arrow_in_the_subtitle_is_drawn(tmp_path):
    import warnings

    route = route_trip(STOPS, "straight")
    poster = Poster(frame=fit(route.coords, 4, 5), features={}, roads=[], sea=[], route=route, stops=STOPS,
                    stop_heights=[None] * 3, passes=[], sights=[], text=PosterText("Spiti", "Manali → Kaza", "", ""))
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*missing from font")
        render(str(tmp_path / "p.png"), poster, load_theme("terracotta"), load_fonts(), dpi=20)


def test_pelennor_fields_theme_and_its_fonts_load():
    theme = load_theme("pelennor_fields")
    fonts = load_fonts(theme)
    assert theme["style"] == "pelennor_fields" and {"title", "runes", "bold"} <= set(fonts)
    assert load_fonts(load_theme("terracotta"))["title"] is not None  # classic themes: title = bold


def test_pelennor_fields_render_offline(tmp_path):
    from routetoposter.pelennor_fields import BorderText

    route = Route([(77.19, 32.24), (77.6, 32.0), (78.07, 32.23), (77.19, 32.24)], [Leg("Manali", "Kaza", 200, 5)])
    frame = fit(route.coords, 4, 5)
    glacier = [[77.4, 32.3], [77.5, 32.3], [77.5, 32.35], [77.4, 32.3]]
    poster = Poster(frame=frame, features={"glacier": [glacier], "primary": [[[77.0, 32.0], [78.2, 32.3]]]},
                    roads=["primary"], sea=[], route=route, stops=STOPS, stop_heights=[2050, 3650, 2050],
                    passes=[], sights=[], text=PosterText("Spiti Valley", "Manali loop", "200 KM", "JUNE 2026"),
                    peaks=[(77.8, 32.4, 5200)],
                    border=BorderText("Manali · Kaza", "four days", None, "Our own words"))
    out = tmp_path / "pelennor_fields.png"
    theme = load_theme("pelennor_fields")
    render(str(out), poster, theme, load_fonts(theme), dpi=50)
    assert Image.open(out).size == (200, 250)


# ── flights and sea crossings ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_coastline_download(monkeypatch):
    """Sailing legs look up the coastline; tests never go online (no coast: drawn straight)."""
    from routetoposter import mapdata
    monkeypatch.setattr(mapdata, "fetch_sea", lambda bbox: [])


# Guayaquil, by road to Salinas, sail to Puerto Lopez, fly to the Galapagos and back.
MIXED = [Stop("Guayaquil", -2.19, -79.89), Stop("Salinas", -2.21, -80.96),
         Stop("Puerto Lopez", -1.56, -80.81, route="sailing"), Stop("Baltra", -0.45, -90.27, route="flying"),
         Stop("Guayaquil", -2.19, -79.89, route="flying")]


def test_a_stop_route_overrides_the_trip_route_for_its_leg():
    route = route_trip(MIXED, "straight")
    assert [leg.mode for leg in route.legs] == ["straight", "sailing", "flying", "flying"]
    assert [seg.mode for seg in route.segments] == ["straight", "sailing", "flying"]  # both flights: one segment
    assert route.coords[0] == (-79.89, -2.19) and route.coords[-1] == (-79.89, -2.19)
    assert [s.coords[0] for s in route.segments[1:]] == [s.coords[-1] for s in route.segments[:-1]]  # joined up
    assert route.ground_lines() == [route.segments[0].coords]


def test_a_flight_is_an_arc_bowing_left_of_the_way_flown():
    from routetoposter.route import ARC_BEND, flight_arc

    there = flight_arc(Stop("A", 0.0, 0.0), Stop("B", 0.0, 10.0))  # flying east
    assert there[0] == (0.0, 0.0) and there[-1] == (10.0, 0.0)
    mid_lon, mid_lat = there[len(there) // 2]
    assert abs(mid_lon - 5) < 1e-9 and abs(mid_lat - 10 * ARC_BEND) < 1e-9  # left of east is north
    back = flight_arc(Stop("B", 0.0, 10.0), Stop("A", 0.0, 0.0))
    assert back[len(back) // 2][1] < 0  # the way back is a second arc, not the same line


def test_flights_and_sea_crossings_in_the_description():
    lines = describe(route_trip(MIXED, "straight"), MIXED)
    assert "by boat" in lines[1] and "flight" in lines[2]
    total = next(line for line in lines if line.startswith("Total"))
    assert "km straight line, " in total and "km by boat, " in total and "km flight)" in total
    assert "loop (starts and ends in Guayaquil)" in total


def test_passes_and_altitudes_only_along_the_ground(monkeypatch):
    from routetoposter import extras

    sea = [[(0.0, 0.0), (1.0, 0.0)]]
    land = [[(0.0, 1.0), (1.0, 1.0)], [(5.0, 1.0), (6.0, 1.0)]]
    passes = [Pass("Over the gap", 3.0, 1.0, 4000), Pass("On land", 5.5, 1.0, 3000)]
    assert [p.name for p in extras.passes_on_route(passes, land)] == ["On land"]  # no line across the gap
    assert extras.passes_on_route(passes, []) == []
    monkeypatch.setattr(extras, "elevations", lambda points: [100.0 * (i + 1) for i in range(len(points))])
    assert extras.altitudes([(0.0, 0.0), (1.0, 0.0)], [], [])[1:] == (None, 200)  # no land: highest is a stop
    assert len(extras.resample_lines(land + sea, 30)) >= 30


def test_render_flights_and_sea_crossings_offline(tmp_path):
    route = route_trip(MIXED, "straight")
    frame = fit(route.coords, 4, 5)
    poster = Poster(frame=frame, features={}, roads=[], sea=[], route=route, stops=MIXED,
                    stop_heights=[None] * len(MIXED), passes=[], sights=[],
                    text=PosterText("Ecuador", "Guayaquil · Galapagos", "", ""))
    out = tmp_path / "poster.png"
    render(str(out), poster, load_theme("terracotta"), load_fonts(), dpi=50)
    assert Image.open(out).size == (200, 250)


def test_a_server_that_is_down_is_skipped_at_once(monkeypatch):
    from routetoposter import mapdata

    asked, notices = [], []
    monkeypatch.setattr(mapdata, "_reachable", lambda url: url != mapdata.OVERPASS)
    monkeypatch.setattr(mapdata, "fetch_json", lambda url, *a, **k: asked.append(url) or {"elements": []})
    assert mapdata.overpass("q", notice=notices.append) == {"elements": []}
    assert asked == [mapdata.OVERPASS_BACKUPS[0]]  # the main server was never waited for
    assert notices[0].startswith("overpass-api.de isn't answering") and "backup" in notices[-1]


def test_light_lookups_wait_briefly_and_ask_every_server(monkeypatch):
    from routetoposter import mapdata

    asked = []

    def slow(url, body, timeout, retries):
        asked.append((url, timeout))
        raise TimeoutError("timed out")

    monkeypatch.setattr(mapdata, "_reachable", lambda url: True)
    monkeypatch.setattr(mapdata, "fetch_json", slow)
    with pytest.raises(mapdata.UserError):
        mapdata.overpass("q", patient=False, light=True)
    assert asked == [(url, mapdata.LIGHT_TIMEOUT) for url in mapdata.overpass_servers()]


def test_peaks_come_from_wikidata_when_overpass_fails(monkeypatch):
    from routetoposter import extras
    from routetoposter.errors import UserError

    monkeypatch.setattr(extras, "_overpass_gave_up", [0.0])

    def down(*a, **k):
        raise UserError("busy")

    answer = {"results": {"bindings": [
        {"item": {"value": "Q1"}, "coord": {"value": "Point(-9.7428 51.9989)"}, "ele": {"value": "1038.6"}},
        {"item": {"value": "Q1"}, "coord": {"value": "Point(-9.7428 51.9989)"}, "ele": {"value": "1038.6"}}]}}
    monkeypatch.setattr(extras, "overpass", down)
    monkeypatch.setattr(extras, "fetch_json", lambda url, timeout: answer)
    assert extras.fetch_peaks((51.5, -10.0, 52.5, -9.0)) == [(-9.7428, 51.9989, 1038.6)]
    monkeypatch.setattr(extras, "fetch_json", lambda *a, **k: pytest.fail("saved"))
    assert len(extras.fetch_peaks((51.5, -10.0, 52.5, -9.0))) == 1
    monkeypatch.setattr(extras, "overpass", lambda *a, **k: pytest.fail("Overpass just failed: not asked again"))
    monkeypatch.setattr(extras, "fetch_json", lambda url, timeout: answer)
    extras.fetch_peaks((53.5, -10.0, 54.5, -9.0))  # another square


def test_own_overpass_server(monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_OVERPASS", "http://localhost:12345/api/interpreter, https://b/api/interpreter")
    assert mapdata.overpass_servers() == ["http://localhost:12345/api/interpreter", "https://b/api/interpreter"]
    monkeypatch.delenv("ROUTETOPOSTER_OVERPASS")
    assert mapdata.overpass_servers()[0] == mapdata.OVERPASS


def test_download_progress_without_a_terminal(capsys):
    from routetoposter.build import Progress, _clock

    progress = Progress(2)  # pytest's output isn't a terminal: plain lines
    progress.notice("waiting for overpass-api.de")
    progress.notice("overpass-api.de is busy (504); retrying in 2s")
    progress.part_done(1, 2)
    progress.close()
    assert capsys.readouterr().out.splitlines() == ["     overpass-api.de is busy (504); retrying in 2s", "     part 1/2"]
    assert (_clock(5), _clock(75)) == ("5s", "1m15s")



def island_sea(bbox):
    """Sea everywhere except a square island between lon 0.4 and 0.6, lat -0.15 and 0.15."""
    s, w, n, e = bbox
    outline = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    hole = [[0.4, -0.15], [0.4, 0.15], [0.6, 0.15], [0.6, -0.15], [0.4, -0.15]]
    return [[outline, hole]]


def test_sailing_goes_round_the_land_in_curves():
    from shapely.geometry import LineString, box

    from routetoposter.sailing import sea_route

    line = sea_route(Stop("West", 0.0, 0.0), Stop("East", 0.0, 1.0), island_sea)
    assert line[0] == (0.0, 0.0) and line[-1] == (1.0, 0.0)
    assert not LineString(line).intersects(box(0.4, -0.15, 0.6, 0.15))  # never over the island
    assert len(line) > 10  # the turns are rounded, not sharp corners


def test_open_water_is_a_gentle_arc_and_no_coastline_is_straight(monkeypatch):
    from routetoposter import mapdata
    from routetoposter.sailing import sea_route

    arc = sea_route(Stop("A", 1.0, 0.0), Stop("B", 1.0, 1.0), island_sea)  # north of the island: open water
    assert 1.0 < arc[len(arc) // 2][1] < 1.1  # bows a little to the left (north) of the way sailed (east)
    assert sea_route(Stop("A", 1.0, 0.0), Stop("B", 1.0, 1.0), lambda bbox: []) is None
    monkeypatch.setattr(mapdata, "fetch_sea", lambda bbox: [])
    route = route_trip([Stop("A", 1.0, 0.0), Stop("B", 1.0, 1.0, route="sailing")], "driving")
    assert route.coords == [(0.0, 1.0), (1.0, 1.0)] and "drawn straight" in route.warnings[0]


def test_a_lookup_shows_what_the_server_is_doing(monkeypatch, capsys):
    from routetoposter import mapdata
    from routetoposter.build import optional

    monkeypatch.setattr(mapdata, "_reachable", lambda url: url != mapdata.OVERPASS)
    monkeypatch.setattr(mapdata, "fetch_json", lambda url, *a, **k: {"elements": [1]})
    assert optional("mountain peaks", lambda: mapdata.overpass("q", patient=False)["elements"], []) == [1]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "     overpass-api.de isn't answering; trying another server"
    assert out[-1].startswith("     ✓ mountain peaks (")


# ── far-away stops off the map ───────────────────────────────────────────────────────────────────

OFF_MAP = [Stop("Quito", -0.20, -78.49), Stop("Guayaquil", -2.19, -79.89),
           Stop("Galapagos", -0.45, -90.27, route="flying", off_map=True)]


def test_an_off_map_stop_is_left_out_of_the_fit_and_the_ground():
    route = route_trip(OFF_MAP, "straight")
    assert [(seg.mode, seg.off_map) for seg in route.segments] == [("straight", False), ("flying", True)]
    on_map = [(-78.49, -0.20), (-79.89, -2.19)]
    assert route.fit_coords()[:2] == on_map and route.ground_lines() == [on_map]
    lead = route.fit_coords()[2]  # a little way out along the flight, so the map reaches toward it
    assert len(route.fit_coords()) == 3 and -80.5 < lead[0] < -80.0  # west of Guayaquil, by ~15% of the trip
    assert route.km > 1300  # the flight is part of the whole trip…
    assert route.on_map_km == route.legs[0].km < 300  # …but not of the distance on the poster
    assert stats_line(Trip(stops=OFF_MAP), route, [], None).startswith(f"{route.legs[0].km:,.0f} KM")


def test_a_line_to_an_off_map_stop_is_cut_at_the_edge_going_out_or_coming_in():
    import numpy as np

    from routetoposter.render import _on_map_part

    line = np.array([[1.0, 1.0], [5.0, 1.0]])  # from (1, 1) east, off a map that ends at x = 3
    part, exit_point = _on_map_part(line, (0, 0, 3, 2), 1)
    assert part[0].tolist() == [1.0, 1.0] and abs(exit_point[0] - 3) < 0.05
    part, exit_point = _on_map_part(line[::-1], (0, 0, 3, 2), 1)  # coming in
    assert part[-1].tolist() == [1.0, 1.0] and abs(exit_point[0] - 3) < 0.05
    assert _on_map_part(line + 10, (0, 0, 3, 2), 1) == (None, None)


def test_render_with_an_off_map_stop_offline(tmp_path):
    route = route_trip(OFF_MAP, "straight")
    frame = fit(route.fit_coords(), 4, 5)
    for theme in ("terracotta", "pelennor_fields"):
        t = load_theme(theme)
        poster = Poster(frame=frame, features={}, roads=[], sea=[], route=route, stops=OFF_MAP,
                        stop_heights=[None] * 3, passes=[], sights=[], text=PosterText("Ecuador", "", "", ""),
                        border=BorderText("Quito", "Guayaquil", None, None))
        render(str(tmp_path / f"{theme}.png"), poster, t, load_fonts(t), dpi=50)


def test_out_and_back_to_an_off_map_stop_is_two_flights(tmp_path, monkeypatch):
    from routetoposter import render as render_module

    stops = OFF_MAP + [Stop("Quito", -0.20, -78.49, route="flying")]
    route = route_trip(stops, "straight")
    assert [(seg.mode, seg.off_map) for seg in route.segments] == [("straight", False), ("flying", True)]
    labels = []
    monkeypatch.setattr(render_module, "_edge_label", lambda ax, point, name, km, *a: labels.append((name, round(km))) or (0, 0, 0, 0))
    frame = fit(route.fit_coords(), 4, 5)
    poster = Poster(frame=frame, features={}, roads=[], sea=[], route=route, stops=stops, stop_heights=[None] * 4,
                    passes=[], sights=[], text=PosterText("Ecuador", "", "", ""))
    render_module.render(str(tmp_path / "p.png"), poster, load_theme("terracotta"), load_fonts(), dpi=20)
    assert [name for name, _ in labels] == ["Galapagos", "Galapagos"]  # out, and back: a label each
    assert [km for _, km in labels] == [round(leg.km) for leg in route.legs[1:]]  # each flight's own, as in the summary


def test_a_run_of_sea_legs_gets_one_boat(tmp_path, monkeypatch):
    from routetoposter import mapdata
    from routetoposter import render as render_module

    monkeypatch.setattr(mapdata, "fetch_sea", lambda bbox: [])  # sea legs drawn straight, offline
    stops = [Stop("A", 0.0, 0.0), Stop("B", 0.0, 0.3, route="sailing"), Stop("C", 0.2, 0.5, route="sailing"),
             Stop("D", 0.4, 0.4, route="sailing"), Stop("E", 0.5, 0.6), Stop("F", 0.9, 0.9, route="flying"),
             Stop("G", 1.0, 1.2, route="sailing")]
    route = route_trip(stops, "straight")
    assert [seg.mode for seg in route.segments] == ["sailing", "straight", "flying", "sailing"]
    boats = []
    monkeypatch.setattr(render_module, "_boat", lambda ax, x, y, **kw: boats.append((x, y)))
    frame = fit(route.fit_coords(), 4, 5)
    poster = Poster(frame=frame, features={}, roads=[], sea=[], route=route, stops=stops, stop_heights=[None] * 7,
                    passes=[], sights=[], text=PosterText("Islands", "", "", ""))
    render_module.render(str(tmp_path / "p.png"), poster, load_theme("terracotta"), load_fonts(), dpi=20)
    assert len(boats) == 2  # A→B→C→D is one voyage, F→G another


def test_a_sea_crossing_is_planned_once(monkeypatch):
    from routetoposter import route as route_module

    planned = []
    monkeypatch.setattr(route_module, "sea_route",
                        lambda a, b, sea_for: planned.append(b.name) or [(a.lon, a.lat), (0.5, 0.2), (b.lon, b.lat)])
    stops = [Stop("A", 0.0, 0.0), Stop("B", 1.0, 1.0, route="sailing"), Stop("C", 1.0, 2.0, route="sailing")]
    first = route_trip(stops, "straight")
    assert route_trip(stops, "straight").coords == first.coords and planned == ["B", "C"]
    stops[2] = Stop("C", 1.2, 2.0, route="sailing")  # moved: only its own crossing again
    route_trip(stops, "straight")
    assert planned == ["B", "C", "C"]


def _fake_osrm(asked):
    def fetch_json(url, *a, **k):
        asked.append(url)
        points = [[float(v) for v in p.split(",")] for p in url.split("/driving/")[1].split("?")[0].split(";")]
        return {"code": "Ok", "waypoints": [{"distance": 0}] * len(points),
                "routes": [{"geometry": {"coordinates": points}, "legs": [{"distance": 1000, "duration": 60}] * (len(points) - 1)}]}
    return fetch_json


def test_moving_a_stop_only_routes_its_own_legs_again(monkeypatch):
    from routetoposter import route as route_module

    asked = []
    monkeypatch.setattr(route_module, "fetch_json", _fake_osrm(asked))
    stops = [Stop("A", 0.0, 0.0), Stop("B", 0.1, 0.1), Stop("C", 0.2, 0.2), Stop("D", 0.3, 0.3)]
    assert len(route_trip(stops, "driving").legs) == 3 and len(asked) == 3  # one request per leg
    stops[2] = Stop("C", 0.25, 0.2)
    route = route_trip(stops, "driving")
    assert len(asked) == 5 and "0.200000,0.250000" in asked[3] and "0.200000,0.250000" in asked[4]  # B→C, C→D
    assert route.coords == [(0.0, 0.0), (0.1, 0.1), (0.2, 0.25), (0.3, 0.3)] and not route.warnings


def test_a_run_saved_whole_by_an_earlier_version_is_still_used(monkeypatch):
    from routetoposter import route as route_module
    from routetoposter.net import cached_json

    stops = [Stop("A", 0.0, 0.0), Stop("B", 0.1, 0.1), Stop("C", 0.2, 0.2)]
    whole = route_module._osrm_url(stops)
    cached_json("osrm", whole, lambda: _fake_osrm([])(whole))
    monkeypatch.setattr(route_module, "fetch_json", lambda *a, **k: pytest.fail("routed again"))
    assert len(route_trip(stops, "driving").legs) == 2


def test_an_untrusted_certificate_stops_at_once_with_a_clear_message(monkeypatch):
    import ssl
    import urllib.error
    import urllib.request

    from routetoposter import net
    from routetoposter.net import CertificateProblem

    tries = []

    def refuse(request, timeout):
        tries.append(request.full_url)
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_message = "self-signed certificate in certificate chain"
        raise urllib.error.URLError(error)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(net.time, "sleep", lambda s: pytest.fail("a certificate error was retried"))
    with pytest.raises(CertificateProblem, match="company network or VPN"):
        route_trip([Stop("A", 0.0, 0.0), Stop("B", 0.1, 0.1)], "driving")
    assert len(tries) == 1  # not retried, and no fallback to Valhalla or a straight line
