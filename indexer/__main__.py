"""Build / incrementally update the code search index.

Usage:
  python -m indexer --org ippoan --db code-index.db [--workdir .repos]
                    [--full] [--only repo1,repo2] [--checkpoint]

Work is repo-atomic: each repo is chunked, embedded, inserted and its commit
recorded before moving on, so an interrupted build loses at most the repo in
flight. With --checkpoint the DB is also gzip-uploaded to the GitHub release
`index-checkpoint` every --checkpoint-interval seconds, so even a killed
runner leaves resumable progress behind.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import os
import shutil
import subprocess
import sys
import time

from . import chunker, db as dbm, gitsync

# Embed only the head of each chunk: CPU attention cost/memory grows
# quadratically with sequence length, and full 8000-char chunks killed a
# 16GB Actions runner. The full text is still stored in the DB for display.
EMBED_MAX_CHARS = 2000
CHECKPOINT_RELEASE = "index-checkpoint"

_model = None


def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        cache_dir = os.environ.get("FASTEMBED_CACHE") or os.path.expanduser(
            "~/.cache/fastembed")
        _model = TextEmbedding(dbm.MODEL_NAME, cache_dir=cache_dir)
    return _model


class Checkpointer:
    """Periodically upload the DB to the checkpoint release via `gh`."""

    def __init__(self, db_path: str, enabled: bool, interval: int):
        self.db_path = db_path
        self.enabled = enabled
        self.interval = interval
        self.last = time.time()
        self.release_ready = False

    def maybe(self, db, force: bool = False):
        if not self.enabled:
            return
        if not force and time.time() - self.last < self.interval:
            return
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # asset name must equal the published one so the download step is uniform
        gz = self.db_path + ".gz"
        with open(self.db_path, "rb") as src, gzip.open(gz, "wb", compresslevel=5) as dst:
            shutil.copyfileobj(src, dst)
        if not self.release_ready:
            subprocess.run(
                ["gh", "release", "create", CHECKPOINT_RELEASE,
                 "--title", "index checkpoint (in-progress build)",
                 "--notes", "Partial index uploaded periodically so an "
                            "interrupted build can resume."],
                check=False, capture_output=True,
            )
            self.release_ready = True
        r = subprocess.run(
            ["gh", "release", "upload", CHECKPOINT_RELEASE, gz, "--clobber"],
            check=False, capture_output=True, text=True,
        )
        status = "ok" if r.returncode == 0 else f"failed: {r.stderr.strip()}"
        print(f"  checkpoint upload {status}", flush=True)
        self.last = time.time()


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--db", default="code-index.db")
    ap.add_argument("--workdir", default=".repos")
    ap.add_argument("--full", action="store_true", help="ignore previous state, rebuild all")
    ap.add_argument("--only", default="", help="comma-separated repo names")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--checkpoint", action="store_true",
                    help="periodically upload the DB to the checkpoint release")
    ap.add_argument("--checkpoint-interval", type=int, default=600,
                    help="seconds between checkpoint uploads")
    ap.add_argument("--parallel-threshold", type=int, default=1000,
                    help="use all CPU cores (worker processes) when a repo has "
                         "at least this many chunks; 0 disables parallelism")
    return ap.parse_args(argv)


def collect_repo_work(db, args, name: str) -> tuple[str, list] | None:
    """Sync one repo; returns (head_sha, pending_chunks) or None to skip."""
    head = gitsync.sync_repo(args.workdir, args.org, name)
    old = dbm.get_repo_commit(db, name)
    if old == head:
        return None

    changed = None
    if old is not None:
        changed = gitsync.changed_files(args.workdir, name, old, head)

    if changed is None:
        dbm.delete_repo(db, name)
        files = [(p, "A") for p in gitsync.list_files(args.workdir, name)]
        mode = "full"
    else:
        files = [(p, s) for s, p in changed]
        mode = f"diff {len(files)} files"
    print(f"[{name}] {old or 'new'} -> {head} ({mode})", flush=True)

    pending = []
    for path, status in files:
        if not gitsync.wanted(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        lang = chunker.chunk_lang(ext)
        if not lang:
            continue
        if changed is not None:
            dbm.delete_path(db, name, path)
        if status == "D":
            continue
        text = gitsync.read_text(args.workdir, name, path)
        if text is None:
            continue
        for ch in chunker.chunk_file(text, ext):
            pending.append((path, ch, lang))
    return head, pending


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

    ckpt = Checkpointer(args.db, args.checkpoint, args.checkpoint_interval)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    stats = {"repos_updated": 0, "chunks_added": 0}

    for name in repos:
        try:
            work = collect_repo_work(db, args, name)
        except Exception as e:
            print(f"[{name}] failed: {e}", file=sys.stderr, flush=True)
            continue
        if work is None:
            continue
        head, pending = work

        if pending:
            print(f"[{name}] embedding {len(pending)} chunks", flush=True)
            texts = [f"{name}/{path}\n{ch.text[:EMBED_MAX_CHARS]}"
                     for path, ch, _ in pending]
            # parallel=0 -> data-parallel worker processes on all cores;
            # single-process embedding measured only ~2 chunks/s on a
            # 4-core runner, which cannot finish a full build in one job.
            # Workers are only worth their startup cost for big repos.
            parallel = 0 if (args.parallel_threshold
                             and len(pending) >= args.parallel_threshold) else None
            done, t0 = 0, time.time()
            for (path, ch, lang), vec in zip(
                pending,
                get_model().embed(texts, batch_size=args.batch_size, parallel=parallel),
            ):
                dbm.insert_chunk(db, name, path, ch, lang, vec)
                done += 1
                if done % 500 == 0:
                    rate = done / (time.time() - t0)
                    print(f"  [{name}] {done}/{len(pending)} ({rate:.0f}/s)",
                          flush=True)
                    db.commit()
                    ckpt.maybe(db)
            stats["chunks_added"] += len(pending)

        # repo is complete only once its commit is recorded — crash before
        # this line makes the next run redo the repo from its previous state
        dbm.set_repo_commit(db, name, head, now)
        db.commit()
        ckpt.maybe(db)
        stats["repos_updated"] += 1

    dbm.set_meta(db, "updated_at", now)
    db.commit()
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    print(
        f"done: {stats['repos_updated']} repos updated, "
        f"+{stats['chunks_added']} chunks, total {total}",
        flush=True,
    )
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
