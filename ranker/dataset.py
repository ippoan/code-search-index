"""Training data for the injection decision, mined from git history.

One past commit = one training query: its subject is the "instruction", and
the code files it touched are what should have been put in front of whoever
wrote it. Each query is run through the normal index search
(`indexer.search.search`), the top-k chunks become the candidates, and a
candidate is positive when its file was changed by that commit.

No LLM is involved. Labels are file-level: a chunk in a touched file counts
as positive even if that particular function was not the part that changed,
which is the main known source of label noise.

The output is a JSONL file of query groups. It stays out of git — commit
subjects of private work can name internal hosts (see CLAUDE.md).

Usage:
  python -m ranker.dataset --db code-index.db --workdir ~/src \
      --since 2025-01-01 --out dataset.jsonl [--k 30]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

from indexer import chunker, gitsync
from indexer.db import MODEL_NAME
from indexer.dedup import EXCLUDE_RE
from indexer.search import open_index, search

# Commits that are not somebody describing a change they made.
SKIP_SUBJECT_RE = re.compile(
    r"^(merge |revert|bump |chore\(deps\)|initial commit|wip$|update readme$)",
    re.I)
PR_SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")
MAX_FILES = 10  # a 40-file sweep says nothing about "which chunk to inject"
MIN_QUERY_CHARS = 12


def indexed_repos(db) -> list[str]:
    return [r[0] for r in db.execute("SELECT DISTINCT repo FROM chunks ORDER BY repo")]


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def local_clones(workdir: str, slugs) -> dict[str, str]:
    """slug -> directory name under workdir, for clones whose origin matches."""
    out: dict[str, str] = {}
    want = {s.lower(): s for s in slugs}
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        try:
            url = _git(path, "config", "--get", "remote.origin.url")
        except subprocess.CalledProcessError:
            continue
        slug = re.sub(r"^.*github\.com[:/]", "", url).removesuffix(".git").lower()
        if slug in want:
            out[want[slug]] = name
    return out


def default_ref(repo_dir: str) -> str:
    for ref in ("origin/HEAD", "origin/main", "origin/master", "HEAD"):
        try:
            _git(repo_dir, "rev-parse", "--verify", "--quiet", ref)
            return ref
        except subprocess.CalledProcessError:
            continue
    return "HEAD"


def clean_query(subject: str) -> str | None:
    s = PR_SUFFIX_RE.sub("", subject).strip()
    if SKIP_SUBJECT_RE.match(s) or len(s) < MIN_QUERY_CHARS:
        return None
    return s


def indexable_files(changed) -> list[str]:
    """Paths of added/modified files that the index actually holds chunks for.

    Deletions cannot be injected, and test/vendored/generated paths are
    excluded the same way `indexer.dedup` excludes them. Markdown and config
    languages stay in — they are indexed, so they can legitimately be the
    thing worth showing; `features.is_code_lang` lets the model weigh that.
    """
    out = []
    for status, path in changed or []:
        if status == "D" or not gitsync.wanted(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        if not chunker.chunk_lang(ext) or EXCLUDE_RE.search(path):
            continue
        out.append(path)
    return out


def collect_queries(workdir: str, slug: str, name: str, since: str,
                    until: str = "") -> list[dict]:
    """[{repo, sha, ts, query, files}] for one repo's history window."""
    repo_dir = os.path.join(workdir, name)
    fmt = "%H%x00%ct%x00%s"
    args = ["log", default_ref(repo_dir), "--no-merges", f"--since={since}",
            f"--format={fmt}"]
    if until:
        args.append(f"--until={until}")
    rows = []
    for line in _git(repo_dir, *args).splitlines():
        sha, ts, subject = line.split("\0", 2)
        query = clean_query(subject)
        if not query:
            continue
        try:
            changed = gitsync.changed_files(workdir, name, f"{sha}^", sha)
        except subprocess.CalledProcessError:
            continue
        files = indexable_files(changed)
        if not files or len(files) > MAX_FILES:
            continue
        rows.append({"repo": slug, "sha": sha, "ts": int(ts), "query": query,
                     "files": files})
    return rows


def label(group: dict, hits) -> list[dict]:
    """Candidate dicts with label 1 when the chunk's file was touched."""
    touched = {f"{group['repo']}/{p}" for p in group["files"]}
    return [{"repo": h.repo, "path": h.path, "start_line": h.start_line,
             "end_line": h.end_line, "symbol": h.symbol or "", "lang": h.lang,
             "text_len": h.text_len, "distance": h.distance,
             "label": 1 if h.file in touched else 0} for h in hits]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="index DB (code-index.db)")
    ap.add_argument("--workdir", required=True,
                    help="directory holding clones of the indexed repos")
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--until", default="")
    ap.add_argument("--k", type=int, default=30, help="candidates per query")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    db = open_index(args.db)
    slugs = indexed_repos(db)
    clones = local_clones(os.path.expanduser(args.workdir), slugs)
    print(f"{len(slugs)} indexed repos, {len(clones)} available locally",
          flush=True)

    t0 = time.perf_counter()
    groups: list[dict] = []
    for slug, name in sorted(clones.items()):
        rows = collect_queries(os.path.expanduser(args.workdir), slug, name,
                               args.since, args.until)
        print(f"  {slug}: {len(rows)} queries", flush=True)
        groups.extend(rows)
    mine_s = time.perf_counter() - t0
    if not groups:
        print("no queries mined", file=sys.stderr)
        return 1

    from fastembed import TextEmbedding
    cache_dir = os.environ.get("FASTEMBED_CACHE") or os.path.expanduser(
        "~/.cache/fastembed")
    model = TextEmbedding(MODEL_NAME, cache_dir=cache_dir)

    t0 = time.perf_counter()
    n_pos = 0
    with open(args.out, "w") as f:
        for g, vec in zip(groups, model.embed([g["query"] for g in groups],
                                              batch_size=16)):
            g["candidates"] = label(g, search(db, vec, k=args.k))
            g["n_pos"] = sum(c["label"] for c in g["candidates"])
            n_pos += g["n_pos"] > 0
            del g["files"]  # paths of the commit stay out of the dataset file
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    search_s = time.perf_counter() - t0

    print(f"{len(groups)} queries written to {args.out} "
          f"({n_pos} with >=1 positive in top-{args.k}); "
          f"mining {mine_s:.1f}s, embed+search {search_s:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
