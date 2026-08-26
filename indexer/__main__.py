"""Build / incrementally update the code search index.

Usage:
  python -m indexer --org ippoan,ohishi-exp --db code-index.db [--workdir .repos]
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
import json
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
    ap.add_argument("--org", required=True,
                    help="comma-separated GitHub orgs; repos are keyed org/name")
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
                    help="use worker processes when a repo has at least this "
                         "many chunks; 0 disables parallelism")
    ap.add_argument("--workers", type=int, default=2,
                    help="worker processes for big repos (0 = all cores). "
                         "4 workers exhausted the 16GB runner; default 2")
    return ap.parse_args(argv)


def get_partial(db, repo: str) -> dict | None:
    row = db.execute(
        "SELECT value FROM meta WHERE key=?", (f"partial:{repo}",)).fetchone()
    return json.loads(row[0]) if row else None


def set_partial(db, repo: str, head: str, done: int, total: int):
    dbm.set_meta(db, f"partial:{repo}",
                 json.dumps({"head": head, "done": done, "total": total}))


def clear_partial(db, repo: str):
    db.execute("DELETE FROM meta WHERE key=?", (f"partial:{repo}",))


def collect_repo_work(db, args, name: str) -> tuple[str, list, int] | None:
    """Sync one repo; returns (head_sha, pending_chunks, resume_from) or None.

    resume_from > 0 means the first resume_from chunks of `pending` are already
    inserted by a previous interrupted run on the same commit (chunk order is
    deterministic: sorted ls-files x deterministic chunker) and must be skipped.
    """
    head = gitsync.sync_repo(args.workdir, name)
    old = dbm.get_repo_commit(db, name)
    if old == head:
        clear_partial(db, name)
        return None

    changed = None
    if old is not None:
        changed = gitsync.changed_files(args.workdir, name, old, head)

    partial = get_partial(db, name)
    resume = (changed is None and partial is not None
              and partial.get("head") == head)
    if changed is None:
        if not resume:
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

    resume_from = 0
    if resume:
        if partial.get("total") == len(pending) and 0 < partial.get("done", 0) <= len(pending):
            resume_from = partial["done"]
        else:
            # chunking no longer matches the recorded progress — start over
            dbm.delete_repo(db, name)
            clear_partial(db, name)
    return head, pending, resume_from


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

    repos = sorted(
        f"{org}/{n}"
        for org in args.org.split(",") if org
        for n in gitsync.list_public_repos(org)
    )
    if args.only:
        only = set(args.only.split(","))
        repos = [r for r in repos if r in only]
    print(f"{len(repos)} repos to consider", flush=True)

    # Drop repos that are no longer listed (deleted / renamed / org removed).
    # Also migrates pre-multi-org DBs whose keys were bare names.
    if not args.only:
        known = set(repos)
        for (stale,) in db.execute("SELECT repo FROM repos").fetchall():
            if stale not in known:
                print(f"[{stale}] no longer listed -> removing", flush=True)
                dbm.delete_repo(db, stale)
                db.execute("DELETE FROM repos WHERE repo=?", (stale,))
                clear_partial(db, stale)
        db.commit()

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
        head, pending, resume_from = work
        todo = pending[resume_from:]

        if todo:
            note = f" (resuming at {resume_from})" if resume_from else ""
            print(f"[{name}] embedding {len(todo)}/{len(pending)} chunks{note}",
                  flush=True)
            texts = [f"{name}/{path}\n{ch.text[:EMBED_MAX_CHARS]}"
                     for path, ch, _ in todo]
            # Data-parallel worker processes for big repos: single-process
            # embedding measured only ~2 chunks/s on a 4-core runner (cannot
            # finish a full build in one job), while all-cores workers (4)
            # exhausted the 16GB runner VM. Small repos skip the startup cost.
            parallel = (args.workers if (args.parallel_threshold
                        and len(todo) >= args.parallel_threshold) else None)
            done, t0 = 0, time.time()
            for (path, ch, lang), vec in zip(
                todo,
                get_model().embed(texts, batch_size=args.batch_size, parallel=parallel),
            ):
                dbm.insert_chunk(db, name, path, ch, lang, vec)
                done += 1
                if done % 500 == 0:
                    rate = done / (time.time() - t0)
                    print(f"  [{name}] {resume_from + done}/{len(pending)} "
                          f"({rate:.0f}/s)", flush=True)
                    # record intra-repo progress so an interrupted run resumes
                    # this repo from here instead of redoing it from scratch
                    set_partial(db, name, head, resume_from + done, len(pending))
                    db.commit()
                    ckpt.maybe(db)
            stats["chunks_added"] += len(todo)

        # repo is complete only once its commit is recorded — crash before
        # this line makes the next run resume the repo from its partial marker
        clear_partial(db, name)
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
