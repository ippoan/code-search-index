"""Vector search over the index DB — the one place that holds the vec SQL.

`indexer.similar` (PR-time duplicate check) and `ranker` (training-data
candidate generation) both go through `search()`. `mcp/server.py` keeps its
own copy: it is started as `python mcp/server.py`, and putting the repo root
on sys.path so it could `import indexer` would also put the repo's `mcp/`
directory ahead of the installed `mcp` SDK package.
"""
from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass

from .db import DIMS


@dataclass(frozen=True)
class Hit:
    repo: str
    path: str
    start_line: int
    end_line: int
    symbol: str
    lang: str
    text_len: int
    distance: float

    @property
    def file(self) -> str:
        return f"{self.repo}/{self.path}"

    @property
    def cos(self) -> float:
        return cos_from_distance(self.distance)


def cos_from_distance(distance: float) -> float:
    """Embeddings are L2-normalised, so dist^2 = 2 - 2*cos."""
    return 1.0 - (distance * distance) / 2.0


def open_index(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def search(db: sqlite3.Connection, vec, k: int = 20) -> list[Hit]:
    """Top-k nearest chunks for one embedding, nearest first."""
    rows = db.execute(
        "SELECT c.repo, c.path, c.start_line, c.end_line, c.symbol, c.lang, "
        "length(c.text), v.distance "
        "FROM (SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? "
        "      ORDER BY distance LIMIT ?) v JOIN chunks c ON c.id = v.rowid "
        "ORDER BY v.distance",
        (struct.pack(f"{DIMS}f", *vec), k),
    ).fetchall()
    return [Hit(*r) for r in rows]
