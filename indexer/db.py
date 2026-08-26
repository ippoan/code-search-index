"""sqlite-vec backed index storage."""
from __future__ import annotations

import sqlite3
import struct

DIMS = 768
MODEL_NAME = "jinaai/jina-embeddings-v2-base-code"


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS repos(
            repo TEXT PRIMARY KEY, commit_sha TEXT, indexed_at TEXT);
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY,
            repo TEXT, path TEXT,
            start_line INT, end_line INT,
            symbol TEXT, lang TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS idx_chunks_repo_path ON chunks(repo, path);
        """
    )
    db.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{DIMS}])"
    )
    return db


def model_matches(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    return row is None or row[0] == MODEL_NAME


def set_meta(db: sqlite3.Connection, key: str, value: str):
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def delete_path(db: sqlite3.Connection, repo: str, path: str) -> int:
    ids = [r[0] for r in db.execute(
        "SELECT id FROM chunks WHERE repo=? AND path=?", (repo, path))]
    for cid in ids:
        db.execute("DELETE FROM vec_chunks WHERE rowid=?", (cid,))
    db.execute("DELETE FROM chunks WHERE repo=? AND path=?", (repo, path))
    return len(ids)


def delete_repo(db: sqlite3.Connection, repo: str) -> int:
    ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE repo=?", (repo,))]
    for cid in ids:
        db.execute("DELETE FROM vec_chunks WHERE rowid=?", (cid,))
    db.execute("DELETE FROM chunks WHERE repo=?", (repo,))
    return len(ids)


def insert_chunk(db: sqlite3.Connection, repo: str, path: str, chunk, lang: str,
                 vector) -> int:
    cur = db.execute(
        "INSERT INTO chunks(repo, path, start_line, end_line, symbol, lang, text) "
        "VALUES(?,?,?,?,?,?,?)",
        (repo, path, chunk.start_line, chunk.end_line, chunk.symbol, lang, chunk.text),
    )
    cid = cur.lastrowid
    db.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
        (cid, struct.pack(f"{DIMS}f", *vector)),
    )
    return cid


def set_repo_commit(db: sqlite3.Connection, repo: str, sha: str, when: str):
    db.execute(
        "INSERT INTO repos(repo, commit_sha, indexed_at) VALUES(?,?,?) "
        "ON CONFLICT(repo) DO UPDATE SET commit_sha=excluded.commit_sha, "
        "indexed_at=excluded.indexed_at",
        (repo, sha, when),
    )


def get_repo_commit(db: sqlite3.Connection, repo: str) -> str | None:
    row = db.execute("SELECT commit_sha FROM repos WHERE repo=?", (repo,)).fetchone()
    return row[0] if row else None
