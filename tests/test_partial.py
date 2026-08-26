from indexer import db as dbm
from indexer.__main__ import clear_partial, get_partial, set_partial


def test_partial_roundtrip(tmp_path):
    db = dbm.open_db(str(tmp_path / "t.db"))
    assert get_partial(db, "r") is None

    set_partial(db, "r", "abc123", 500, 9897)
    assert get_partial(db, "r") == {"head": "abc123", "done": 500, "total": 9897}

    set_partial(db, "r", "abc123", 1000, 9897)
    assert get_partial(db, "r")["done"] == 1000

    clear_partial(db, "r")
    assert get_partial(db, "r") is None


def test_partial_is_per_repo(tmp_path):
    db = dbm.open_db(str(tmp_path / "t.db"))
    set_partial(db, "r1", "aaa", 10, 100)
    set_partial(db, "r2", "bbb", 20, 200)
    clear_partial(db, "r1")
    assert get_partial(db, "r1") is None
    assert get_partial(db, "r2") == {"head": "bbb", "done": 20, "total": 200}
