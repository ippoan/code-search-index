import struct

import pytest

from indexer import chunker, db as dbm
from indexer.merge import main as merge_main


def _chunk(start=1, end=3, symbol="s", text="t"):
    return chunker.Chunk(start_line=start, end_line=end, symbol=symbol, text=text)


def _vec(seed):
    return [seed] * dbm.DIMS


def _make_shard(path, rows, repos):
    db = dbm.open_db(str(path))
    dbm.set_meta(db, "model", dbm.MODEL_NAME)
    for repo, p, seed in rows:
        dbm.insert_chunk(db, repo, p, _chunk(), "rust", _vec(seed))
    for repo, sha in repos:
        dbm.set_repo_commit(db, repo, sha, "2026-08-26T00:00:00+00:00")
    db.commit()
    db.close()


def test_merge_combines_chunks_vectors_and_repos(tmp_path):
    _make_shard(tmp_path / "s0.db",
                [("r1", "a.rs", 0.1), ("r2", "b.rs", 0.2)],
                [("r1", "aaa"), ("r2", "bbb")])
    _make_shard(tmp_path / "s1.db",
                [("r1", "c.rs", 0.3)],
                [("r1", "aaa"), ("r2", "bbb")])
    out = tmp_path / "out.db"
    assert merge_main(["--out", str(out), str(tmp_path / "s0.db"), str(tmp_path / "s1.db")]) == 0

    db = dbm.open_db(str(out))
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 3
    assert dbm.get_repo_commit(db, "r1") == "aaa"
    # vectors survive the merge: nearest neighbour of 0.3-vector is c.rs
    rows = db.execute(
        "SELECT c.path FROM (SELECT rowid, distance FROM vec_chunks "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 1) v "
        "JOIN chunks c ON c.id = v.rowid",
        (struct.pack(f"{dbm.DIMS}f", *_vec(0.3)),),
    ).fetchall()
    assert rows == [("c.rs",)]


def test_merge_rejects_conflicting_repo_pins(tmp_path):
    _make_shard(tmp_path / "s0.db", [("r1", "a.rs", 0.1)], [("r1", "aaa11111")])
    _make_shard(tmp_path / "s1.db", [("r1", "b.rs", 0.2)], [("r1", "bbb22222")])
    with pytest.raises(SystemExit):
        merge_main(["--out", str(tmp_path / "out.db"),
                    str(tmp_path / "s0.db"), str(tmp_path / "s1.db")])
