"""Route helpers, layout, map detail, the sea, poster text, and an offline render. All offline."""
from PIL import Image

from routetoposter.build import stats_line, subtitle, trip_days
from routetoposter.extras import Pass, Sight
from routetoposter.layout import fit, output_dpi, poster_inches
from routetoposter.mapdata import download_level, make_tiles, roads_to_draw, sea_polygons
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


def test_tiles_cover_the_area():
    tiles = make_tiles((30, 76, 33, 79), 20_000)
    assert len(tiles) > 1
    assert min(t[0] for t in tiles) == 30 and max(t[2] for t in tiles) == 33


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


def test_a_saved_bigger_area_is_reused(tmp_path, monkeypatch):
    from routetoposter.net import remember_area, saved_area

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    remember_area("map-high", (30.0, 76.0, 33.0, 79.0))
    assert saved_area("map-high", (30.5, 76.5, 32.5, 78.5)) == (30.0, 76.0, 33.0, 79.0)  # covered
    assert saved_area("map-high", (29.0, 76.5, 32.5, 78.5)) == (29.0, 76.5, 32.5, 78.5)  # sticks out
    assert saved_area("map-low", (30.5, 76.5, 32.5, 78.5)) == (30.5, 76.5, 32.5, 78.5)  # other detail level


def test_shared_edges_with_rounding_noise_still_count_as_covered(tmp_path, monkeypatch):
    from routetoposter.net import remember_area, saved_area

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    remember_area("map-high", (30.5, 76.86575341102125, 32.8, 78.88426958897878))
    assert saved_area("map-high", (30.6, 76.86575341102120, 32.7, 78.88426958897880))[0] == 30.5


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
    monkeypatch.setattr(extras, "overpass", lambda query, patient=True: queries.append(query) or answer)
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
    monkeypatch.setattr(mapdata, "overpass", lambda query, patient=True: asked.append(query) or {"elements": [road]})

    first = mapdata.fetch_features((31.1, 76.8, 31.4, 77.3), "high")
    assert len(asked) == 2 and len(first["primary"]) == 1  # two squares; the road kept once
    mapdata.fetch_features((31.15, 76.85, 31.35, 77.25), "high")  # a smaller poster: all saved
    assert len(asked) == 2
    mapdata.fetch_features((31.1, 76.8, 31.4, 77.8), "high")  # wider: one more square
    assert len(asked) == 3


def test_a_partly_saved_square_is_used_only_for_its_saved_part(tmp_path, monkeypatch):
    from routetoposter import mapdata

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    square = (31.0, 77.0, 31.5, 77.5)
    mapdata._save_square(square, "high", (31.0, 77.0, 31.5, 77.2), {"primary": []})  # only the west part saved
    assert mapdata.tiles_status((31.1, 77.05, 31.4, 77.15), "high") == (1, 1)
    assert mapdata.tiles_status((31.1, 77.05, 31.4, 77.40), "high") == (0, 1)


def test_a_pass_named_only_mountain_pass_is_left_out():
    from routetoposter.extras import passes_on_route

    route = [(77.0, 34.0), (78.0, 34.0)]
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
