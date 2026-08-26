"""Build / incrementally update the code search index.

Usage:
  python -m indexer --org ippoan --db code-index.db [--workdir .repos]
                    [--full] [--only repo1,repo2]
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

from . import chunker, db as dbm, gitsync

# Embed only the head of each chunk: CPU attention cost/memory grows
# quadratically with sequence length, and full 8000-char chunks killed a
# 16GB Actions runner. The full text is still stored in the DB for display.
EMBED_MAX_CHARS = 2000


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--db", default="code-index.db")
    ap.add_argument("--workdir", default=".repos")
    ap.add_argument("--full", action="store_true", help="ignore previous state, rebuild all")
    ap.add_argument("--only", default="", help="comma-separated repo names")
    ap.add_argument("--batch-size", type=int, default=8)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    os.makedirs(args.workdir, exist_ok=True)

    if args.full and os.path.exists(args.db):
        os.remove(args.db)
    db = dbm.open_db(args.db)
    if not dbm.model_matches(db):
        print("embedding model changed -> full rebuild", flush=True)
        db.close()
        os.remove(args.db)
        db = dbm.open_db(args.db)
    dbm.set_meta(db, "model", dbm.MODEL_NAME)

    repos = gitsync.list_public_repos(args.org)
    if args.only:
        only = set(args.only.split(","))
        repos = [r for r in repos if r in only]
    print(f"{len(repos)} repos to consider", flush=True)

    # pending work: (repo, path, chunk, lang) collected across repos, embedded in batches
    pending: list[tuple[str, str, object, str]] = []
    new_commits: dict[str, str] = {}
    stats = {"repos_updated": 0, "files": 0, "deleted": 0}

    for name in repos:
        try:
            head = gitsync.sync_repo(args.workdir, args.org, name)
        except Exception as e:
            print(f"[{name}] clone/fetch failed: {e}", file=sys.stderr, flush=True)
            continue
        old = dbm.get_repo_commit(db, name)
        if old == head:
            continue

        changed = None
        if old is not None:
            changed = gitsync.changed_files(args.workdir, name, old, head)

        if changed is None:
            stats["deleted"] += dbm.delete_repo(db, name)
            files = [(p, "A") for p in gitsync.list_files(args.workdir, name)]
            mode = "full"
        else:
            files = [(p, s) for s, p in changed]
            mode = f"diff {len(files)} files"
        print(f"[{name}] {old or 'new'} -> {head} ({mode})", flush=True)

        for path, status in files:
            if not gitsync.wanted(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            lang = chunker.chunk_lang(ext)
            if not lang:
                continue
            if changed is not None:
                stats["deleted"] += dbm.delete_path(db, name, path)
            if status == "D":
                continue
            text = gitsync.read_text(args.workdir, name, path)
            if text is None:
                continue
            for ch in chunker.chunk_file(text, ext):
                pending.append((name, path, ch, lang))
            stats["files"] += 1
        new_commits[name] = head
        stats["repos_updated"] += 1

    print(f"{len(pending)} chunks to embed", flush=True)
    if pending:
        from fastembed import TextEmbedding
        cache_dir = os.environ.get("FASTEMBED_CACHE") or os.path.expanduser("~/.cache/fastembed")
        model = TextEmbedding(dbm.MODEL_NAME, cache_dir=cache_dir)
        texts = [f"{repo}/{path}\n{ch.text[:EMBED_MAX_CHARS]}" for repo, path, ch, _ in pending]
        t0, done = time.time(), 0
        for (repo, path, ch, lang), vec in zip(
            pending, model.embed(texts, batch_size=args.batch_size)
        ):
            dbm.insert_chunk(db, repo, path, ch, lang, vec)
            done += 1
            if done % 500 == 0:
                rate = done / (time.time() - t0)
                print(f"  embedded {done}/{len(pending)} ({rate:.0f}/s)", flush=True)
                db.commit()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for name, sha in new_commits.items():
        dbm.set_repo_commit(db, name, sha, now)
    dbm.set_meta(db, "updated_at", now)
    db.commit()

    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    print(
        f"done: {stats['repos_updated']} repos updated, {stats['files']} files, "
        f"+{len(pending)} / -{stats['deleted']} chunks, total {total}",
        flush=True,
    )
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
