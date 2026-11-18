import pytest

from app import config, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test starts from the demo's seeded state, in its own database file."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")  # tests never call the network
    store.init()
    store.reset()
