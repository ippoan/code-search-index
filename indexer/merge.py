"""Merge shard DBs (built by indexer.shard) into one index DB.

Usage:
  python -m indexer.merge --out code-index.db shard-0.db shard-1.db ...
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

from . import db as dbm


def _open_shard(path: str) -> sqlite3.Connection:
    src = sqlite3.connect(path)
    src.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(src)
    src.enable_load_extension(False)
    row = src.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    if not row or row[0] != dbm.MODEL_NAME:
        raise SystemExit(f"{path}: embedding model mismatch ({row and row[0]})")
    return src


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("shards", nargs="+")
    args = ap.parse_args(argv)

    if os.path.exists(args.out):
        os.remove(args.out)
    out = dbm.open_db(args.out)
    dbm.set_meta(out, "model", dbm.MODEL_NAME)

    seen_sha: dict[str, str] = {}
    total = 0
    for path in args.shards:
        src = _open_shard(path)
        for repo, sha, at in src.execute(
            "SELECT repo, commit_sha, indexed_at FROM repos"
        ):
            if repo in seen_sha and seen_sha[repo] != sha:
                raise SystemExit(
                    f"{path}: {repo} pinned at {sha[:8]} but another shard used "
                    f"{seen_sha[repo][:8]} — shards must share one shas file")
            seen_sha[repo] = sha
            dbm.set_repo_commit(out, repo, sha, at)
        n = 0
        for row in src.execute(
            "SELECT c.repo, c.path, c.start_line, c.end_line, c.symbol, c.lang, "
            "c.text, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id"
        ):
            repo, p, s, e, sym, lang, text, emb = row
            cur = out.execute(
                "INSERT INTO chunks(repo, path, start_line, end_line, symbol, lang, text) "
                "VALUES(?,?,?,?,?,?,?)", (repo, p, s, e, sym, lang, text))
            out.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                (cur.lastrowid, emb))
            n += 1
        total += n
        print(f"{path}: +{n} chunks", flush=True)
        src.close()
        out.commit()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    dbm.set_meta(out, "updated_at", now)
    out.commit()
    print(f"merged {len(args.shards)} shards: {total} chunks, "
          f"{len(seen_sha)} repos", flush=True)
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
