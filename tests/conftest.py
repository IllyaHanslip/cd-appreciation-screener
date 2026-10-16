import pytest

from cd_screener import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def release_row():
    """Factory for release dicts with sensible defaults."""

    def make(release_id, *, wants, haves, fmt="CD", source="simulated", **extra):
        row = {
            "release_id": release_id,
            "title": f"Title {release_id}",
            "artist": f"Artist {release_id}",
            "year": 1999,
            "genre": "Rock",
            "style": "Shoegaze",
            "format": fmt,
            "country": "US",
            "wants": wants,
            "haves": haves,
            "source": source,
        }
        row.update(extra)
        return row

    return make
