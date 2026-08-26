"""Build one shard of a full index: every repo, every n-th chunk.

Each of the N parallel shard jobs assembles every repo's chunk list and
embeds only its stride (pending[i::n]) — balanced by construction, no
planning or coordination needed. Shards are short-lived and stateless: a
failed shard is simply re-run; indexer.merge combines the shard DBs.

Two read-only caches (built by the merge job from the previous index, see
indexer/caches.py) skip the deterministic work:
- --chunk-cache: repos whose pinned sha matches are not even cloned
- --vector-cache: chunks whose embedding is cached are not embedded

Usage:
  python -m indexer.shard --shard 3/16 --shas shas.json --db shard.db \
      [--chunk-cache .cache/chunks.json.gz] [--vector-cache .cache/vectors.db]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import struct
import sys
import time

from . import caches, chunker, db as dbm, gitsync


def collect_full(workdir: str, name: str) -> list:
    """All chunks of a repo's working tree, in deterministic order."""
    pending = []
    for path in gitsync.list_files(workdir, name):
        if not gitsync.wanted(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        lang = chunker.chunk_lang(ext)
        if not lang:
            continue
        text = gitsync.read_text(workdir, name, path)
        if text is None:
            continue
        for ch in chunker.chunk_file(text, ext):
            pending.append((path, ch, lang))
    return pending


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--workdir", default=".repos")
    ap.add_argument("--shard", required=True, help="i/n, 0-based (e.g. 3/16)")
    ap.add_argument("--shas", required=True, help="JSON file: {org/name: commit_sha}")
    ap.add_argument("--chunk-cache", default="", help="chunks.json.gz from indexer.caches")
    ap.add_argument("--vector-cache", default="", help="vectors.db from indexer.caches")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--workers", type=int, default=2,
                    help="worker processes (0 = all cores); 4 exhausted a 16GB VM")
    args = ap.parse_args(argv)

    i_str, n_str = args.shard.split("/", 1)
    i, n = int(i_str), int(n_str)
    if not 0 <= i < n:
        print(f"bad --shard {args.shard}", file=sys.stderr)
        return 2
    with open(args.shas) as f:
        shas: dict[str, str] = json.load(f)

    chunk_cache = caches.load_chunk_cache(args.chunk_cache)
    vec_cache = None
    if args.vector_cache and os.path.exists(args.vector_cache):
        vec_cache = caches.open_vector_cache(args.vector_cache)

    os.makedirs(args.workdir, exist_ok=True)
    if os.path.exists(args.db):
        os.remove(args.db)
    db = dbm.open_db(args.db)
    dbm.set_meta(db, "model", dbm.MODEL_NAME)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    mine: list = []  # (repo, path, chunk, lang)
    reused = 0
    for name in sorted(shas):
        sha = shas[name]
        cached = chunk_cache.get(name)
        if cached and cached.get("sha") == sha:
            pending = [
                (path, chunker.Chunk(start_line=s, end_line=e, symbol=sym, text=text), lang)
                for path, s, e, sym, lang, text in cached["chunks"]
            ]
            reused += 1
            src = "chunk-cache"
        else:
            # A shard must cover its stride of every repo — fail loudly (job
            # retry) rather than silently publishing a shard with holes.
            gitsync.sync_repo(args.workdir, name)
            gitsync.checkout_sha(args.workdir, name, sha)
            pending = collect_full(args.workdir, name)
            src = "clone"
        take = pending[i::n]
        mine.extend((name, path, ch, lang) for path, ch, lang in take)
        dbm.set_repo_commit(db, name, sha, now)
        print(f"[{name}] {sha[:8]} {len(take)}/{len(pending)} chunks ({src})",
              flush=True)
    print(f"shard {i}/{n}: {reused}/{len(shas)} repos from chunk cache", flush=True)

    # Split the stride into vector-cache hits and chunks that need the model.
    hits: list = []   # (item, embedding blob)
    misses: list = []
    for item in mine:
        repo, path, ch, _lang = item
        emb = None
        if vec_cache is not None:
            emb = caches.lookup_vector(
                vec_cache, caches.text_hash(chunker.embed_text(repo, path, ch.text)))
        (hits.append((item, emb)) if emb is not None else misses.append(item))

    print(f"shard {i}/{n}: {len(hits)} cached vectors, {len(misses)} to embed",
          flush=True)
    for (repo, path, ch, lang), emb in hits:
        dbm.insert_chunk(db, repo, path, ch, lang,
                         struct.unpack(f"{dbm.DIMS}f", emb))
    db.commit()

    if misses:
        from fastembed import TextEmbedding
        cache_dir = os.environ.get("FASTEMBED_CACHE") or os.path.expanduser(
            "~/.cache/fastembed")
        model = TextEmbedding(dbm.MODEL_NAME, cache_dir=cache_dir)
        texts = [chunker.embed_text(repo, path, ch.text)
                 for repo, path, ch, _ in misses]
        parallel = args.workers if len(misses) >= 1000 else None
        done, t0 = 0, time.time()
        for (repo, path, ch, lang), vec in zip(
            misses, model.embed(texts, batch_size=args.batch_size, parallel=parallel),
        ):
            dbm.insert_chunk(db, repo, path, ch, lang, vec)
            done += 1
            if done % 500 == 0:
                rate = done / (time.time() - t0)
                print(f"  {done}/{len(misses)} ({rate:.0f}/s)", flush=True)

    dbm.set_meta(db, "updated_at", now)
    db.commit()
    print(f"shard {i}/{n} done: {len(mine)} chunks "
          f"({len(hits)} cached, {len(misses)} embedded)", flush=True)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
