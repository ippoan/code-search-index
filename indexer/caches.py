"""Build-acceleration caches derived from a finished index DB.

Chunking is deterministic per (repo, commit, chunker) and embeddings are
deterministic per (model, embed text), so both can be remembered:

- chunk cache: gzip JSON {slug: {"sha": ..., "chunks": [[path, start, end,
  symbol, lang, text], ...]}} — a shard reuses a repo's chunks when its
  pinned sha matches, skipping clone + chunking entirely.
- vector cache: sqlite {hash(model + embed_text) -> float32 blob} — a shard
  embeds only chunks whose vector is not cached (a rebuild without model or
  content changes embeds nothing).

The merge job generates both from the merged DB (single writer); shard jobs
only read them. Regenerate-from-DB also means the chunk cache is ordered by
(path, start_line, end_line) — every shard loads the same file, so stride
slicing stays consistent within a build.

CLI:
  python -m indexer.caches --db code-index.db \
      --chunks .cache/chunks.json.gz --vectors .cache/vectors.db
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys

from . import db as dbm


def text_hash(embed_text: str) -> bytes:
    """Cache key for one embedding — model change invalidates naturally."""
    return hashlib.sha256(
        (dbm.MODEL_NAME + "\0" + embed_text).encode("utf-8", "replace")
    ).digest()


def open_vector_cache(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS vecs(hash BLOB PRIMARY KEY, embedding BLOB)")
    return db


def lookup_vector(cache: sqlite3.Connection, key: bytes) -> bytes | None:
    row = cache.execute(
        "SELECT embedding FROM vecs WHERE hash=?", (key,)).fetchone()
    return row[0] if row else None


def load_chunk_cache(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"chunk cache unreadable ({e}) -> ignoring", file=sys.stderr)
        return {}


def build_caches(index_db_path: str, chunks_out: str, vectors_out: str) -> None:
    from .chunker import embed_text  # local import to avoid cycles

    src = sqlite3.connect(index_db_path)
    src.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(src)
    src.enable_load_extension(False)

    shas = dict(src.execute("SELECT repo, commit_sha FROM repos"))
    data: dict = {slug: {"sha": sha, "chunks": []} for slug, sha in shas.items()}
    for repo, path, s, e, sym, lang, text in src.execute(
        "SELECT repo, path, start_line, end_line, symbol, lang, text "
        "FROM chunks ORDER BY repo, path, start_line, end_line"
    ):
        data.setdefault(repo, {"sha": shas.get(repo, ""), "chunks": []})
        data[repo]["chunks"].append([path, s, e, sym, lang, text])

    os.makedirs(os.path.dirname(chunks_out) or ".", exist_ok=True)
    with gzip.open(chunks_out, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(data, f, ensure_ascii=False)

    if os.path.exists(vectors_out):
        os.remove(vectors_out)
    vec = open_vector_cache(vectors_out)
    n = 0
    for repo, path, text, emb in src.execute(
        "SELECT c.repo, c.path, c.text, v.embedding "
        "FROM chunks c JOIN vec_chunks v ON v.rowid = c.id"
    ):
        vec.execute(
            "INSERT OR REPLACE INTO vecs(hash, embedding) VALUES(?, ?)",
            (text_hash(embed_text(repo, path, text)), emb))
        n += 1
    vec.commit()
    vec.close()
    src.close()
    print(f"caches: {sum(len(v['chunks']) for v in data.values())} chunks "
          f"({len(data)} repos), {n} vectors", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--vectors", required=True)
    args = ap.parse_args(argv)
    build_caches(args.db, args.chunks, args.vectors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
