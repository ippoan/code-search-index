import struct

from indexer import chunker, db as dbm


def _chunk(start=1, end=3, symbol="s", text="t"):
    return chunker.Chunk(start_line=start, end_line=end, symbol=symbol, text=text)


def _vec(seed=0.0):
    return [seed] * dbm.DIMS


def test_roundtrip_insert_query_delete(tmp_path):
    db = dbm.open_db(str(tmp_path / "t.db"))
    cid = dbm.insert_chunk(db, "repo1", "src/a.rs", _chunk(), "rust", _vec(0.5))
    rows = db.execute(
        "SELECT rowid FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (struct.pack(f"{dbm.DIMS}f", *_vec(0.5)),),
    ).fetchall()
    assert rows == [(cid,)]

    assert dbm.delete_path(db, "repo1", "src/a.rs") == 1
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 0


def test_repo_commit_tracking_and_model_meta(tmp_path):
    db = dbm.open_db(str(tmp_path / "t.db"))
    assert dbm.model_matches(db)  # empty db matches any model
    dbm.set_meta(db, "model", "other-model")
    assert not dbm.model_matches(db)
    dbm.set_meta(db, "model", dbm.MODEL_NAME)
    assert dbm.model_matches(db)

    assert dbm.get_repo_commit(db, "r") is None
    dbm.set_repo_commit(db, "r", "abc", "2026-08-26T00:00:00+00:00")
    assert dbm.get_repo_commit(db, "r") == "abc"
    dbm.set_repo_commit(db, "r", "def", "2026-08-26T01:00:00+00:00")
    assert dbm.get_repo_commit(db, "r") == "def"


def test_delete_repo_removes_all_paths(tmp_path):
    db = dbm.open_db(str(tmp_path / "t.db"))
    dbm.insert_chunk(db, "r1", "a.rs", _chunk(), "rust", _vec(0.1))
    dbm.insert_chunk(db, "r1", "b.rs", _chunk(), "rust", _vec(0.2))
    dbm.insert_chunk(db, "r2", "c.rs", _chunk(), "rust", _vec(0.3))
    assert dbm.delete_repo(db, "r1") == 2
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
