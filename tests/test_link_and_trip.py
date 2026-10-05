"""Reading Google Maps links, and writing / reading the trip file. All offline."""
from pathlib import Path

import pytest

from routetoposter.errors import UserError
from routetoposter.gmaps import clean_name, parse_link
from routetoposter.trip import NearbySight, Stop, Trip, load_trip, write_trip

DATA = Path(__file__).parent / "data"
THEMES = ["terracotta", "noir"]


def test_spiti_link_gives_every_stop_with_exact_coordinates():
    link = parse_link((DATA / "spiti_link.txt").read_text())
    assert [s.name for s in link.stops] == ["Manali", "Shoja Valley", "Chitkul", "Kalpa", "Tabo", "Hikkim",
                                            "Langza", "Kaza", "Chandratal Lake", "Manali"]
    assert (link.stops[7].lat, link.stops[7].lon) == (32.2275991, 78.0709903)
    assert link.mode == "driving"


def test_kerala_link():
    link = parse_link((DATA / "kerala_link.txt").read_text())
    assert [s.name for s in link.stops][:3] == ["Bengaluru", "Palakkad", "Thrissur"]
    assert link.stops[0].link_name == "Bengaluru, Karnataka"


def test_stop_typed_as_coordinates():
    url = ("https://www.google.com/maps/dir/12.97,77.59/Kaza,+Himachal+Pradesh/@1,1,8z/"
           "data=!4m8!4m7!1m0!1m5!1m1!1s0x1:0x2!2m2!1d78.07!2d32.22!3e0")
    stops = parse_link(url).stops
    assert (stops[0].lat, stops[0].lon) == (12.97, 77.59)
    assert (stops[1].name, stops[1].lat) == ("Kaza", 32.22)


def test_stop_without_coordinates_is_refused_not_guessed():
    url = "https://www.google.com/maps/dir/Manali/Kaza/@1,1,8z/data=!4m5!4m4!1m0!1m0!3e0"
    with pytest.raises(UserError, match="can't be\n  matched up safely"):
        parse_link(url)


def test_not_a_directions_link():
    with pytest.raises(UserError, match="directions link"):
        parse_link("https://www.google.com/maps/place/Kaza")


@pytest.mark.parametrize("full, short", [
    ("Kaza, Himachal Pradesh 172114", "Kaza"),
    ("Shoja Valley Home Stay, H99C+M69, Shoja", "Shoja Valley"),
    ("Chandratal Lake Outpost - Chhatru, 897C+QR", "Chandratal Lake"),
])
def test_clean_name(full, short):
    assert clean_name(full) == short


def make_trip() -> Trip:
    stops = [Stop("Kochi", 9.93, 76.26, nights=1,
                  sights_nearby=[NearbySight("Fort Kochi"), NearbySight("Mattancherry Palace", kind="palace"),
                                 NearbySight("Cherai Beach", lat=10.14, lon=76.18)]),
             Stop("Munnar: hills", 10.08, 77.05, label=False),
             Stop("Kochi", 9.93, 76.26)]
    return Trip(stops=stops, link="https://www.google.com/maps/dir/x", title="Kerala", dates="Dec 2026")


def test_trip_file_round_trip(tmp_path):
    path = tmp_path / "kerala.yaml"
    text = write_trip(make_trip(), path, ["Kochi → Munnar  130 km"], THEMES)
    assert "#   Kochi → Munnar  130 km" in text
    trip = load_trip(path, THEMES)
    assert trip.title == "Kerala" and trip.dates == "Dec 2026" and trip.subtitle == "auto"
    assert trip.stops[1].name == "Munnar: hills" and trip.stops[1].label is False
    assert trip.stops[0].sights_nearby == make_trip().stops[0].sights_nearby


def write_yaml(tmp_path, text: str) -> Path:
    path = tmp_path / "t.yaml"
    path.write_text(text)
    return path


STOPS = "stops:\n  - {name: A, lat: 1, lon: 2}\n  - {name: B, lat: 3, lon: 4}\n"


@pytest.mark.parametrize("extra, message", [
    ("titel: X\n", 'Did you mean "title"'),
    ("theme: noire\n", 'Did you mean "noir"'),
    ("size: 13x16\n", "Choose one of"),
    ("dpi: high\n", "whole number"),
    ("show: {pases: true}\n", 'Did you mean "passes"'),
])
def test_mistakes_get_clear_messages(tmp_path, extra, message):
    with pytest.raises(UserError, match=message):
        load_trip(write_yaml(tmp_path, STOPS + extra), THEMES)


def test_stop_needs_coordinates(tmp_path):
    with pytest.raises(UserError, match="needs lat and lon"):
        load_trip(write_yaml(tmp_path, "stops:\n  - {name: A}\n  - {name: B, lat: 3, lon: 4}\n"), THEMES)


def test_sight_needs_both_lat_and_lon(tmp_path):
    text = "stops:\n  - {name: A, lat: 1, lon: 2, sights_nearby: [{name: X, lat: 1}]}\n  - {name: B, lat: 3, lon: 4}\n"
    with pytest.raises(UserError, match="both lat and lon"):
        load_trip(write_yaml(tmp_path, text), THEMES)


def test_empty_subtitle_means_none(tmp_path):
    trip = load_trip(write_yaml(tmp_path, STOPS + 'subtitle: ""\n'), THEMES)
    assert trip.subtitle is None


def test_same_meaning_words_are_searched_together():
    from routetoposter.sights import name_pattern, normalise

    assert name_pattern("Key Monastery") == "Key (monastery|gompa|gonpa|gompha)"
    assert name_pattern("Fort Kochi") == "(fort|qila|kila|killa) Kochi"
    assert normalise("Ki Gompa") == "ki monastery"


def test_not_found_sight_gets_did_you_mean(tmp_path, monkeypatch):
    from routetoposter import sights

    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path))
    nearby = [{"name": "Ki Gompa", "lon": 78.01, "lat": 32.29, "kind": "monastery"},
              {"name": "Kaza Gompa", "lon": 78.07, "lat": 32.23, "kind": "monastery"},
              {"name": "Key Maidan", "lon": 78.0, "lat": 32.3, "kind": "sight"},
              {"name": "Petrol Pump", "lon": 78.0, "lat": 32.3, "kind": "sight"}]
    monkeypatch.setattr(sights, "_places_named", lambda pattern, lat, lon: nearby)
    kaza = Stop("Kaza", 32.2276, 78.0710)
    names = sights.suggest("Key Monastery", kaza.lat, kaza.lon)
    assert names[0].startswith("Ki Gompa (")
    assert not any("Petrol" in n for n in names)
    assert sights.not_found_line("Key Monastery", kaza).startswith("'Key Monastery' near Kaza: did you mean Ki Gompa")


def test_a_name_that_only_contains_the_words_is_not_accepted(monkeypatch):
    from routetoposter import sights

    kaza = Stop("Kaza", 32.2276, 78.0710)
    staircase = {"name": "Scalinata per Key Gompa", "lon": 78.01, "lat": 32.29, "kind": "sight"}
    monkeypatch.setattr(sights, "search", lambda *args: [staircase])
    assert sights.locate(NearbySight("Key Monastery"), kaza) is None  # → not found, with suggestions

    beach = {"name": "Fort Kochi Beach", "lon": 76.24, "lat": 9.96, "kind": "beach"}
    monkeypatch.setattr(sights, "search", lambda *args: [staircase, beach])
    assert sights.locate(NearbySight("Fort Kochi"), kaza).sight.lon == 76.24  # close enough: used


def test_ladakh_link():
    stops = parse_link((DATA / "ladakh_link.txt").read_text()).stops
    assert [s.name for s in stops] == ["Leh", "Khardung La", "Nubra Valley", "Pangong Tso", "Chushul",
                                       "Hanle Leh", "Thiksey Monastery", "Leh"]


def test_link_changed_by_the_shell_gets_a_clear_message():
    # What bash makes of !1d…!2d… inside double quotes: the history is pasted in.
    mangled = (DATA / "ladakh_link.txt").read_text().replace("!", "echo hello")
    with pytest.raises(UserError, match="single quotes"):
        parse_link(mangled)


def test_runes_settings_round_trip(tmp_path):
    path = write_yaml(tmp_path, STOPS + "runes_left: Our own words\n")
    trip = load_trip(path, THEMES)
    assert (trip.runes_left, trip.runes_right) == ("Our own words", None)
