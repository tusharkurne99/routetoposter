import pytest


@pytest.fixture(autouse=True)
def own_cache(tmp_path, monkeypatch):
    """Every test gets an empty cache of its own: nothing read from or saved to the real cache/."""
    monkeypatch.setenv("ROUTETOPOSTER_CACHE", str(tmp_path / "cache"))
