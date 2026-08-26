"""PR-time similar-code check: warn when newly added code already exists.

Runs inside a PR checkout. Extracts chunks that overlap lines *added* since
--base, embeds them, and queries the published index DB for the nearest
chunk outside this repository. Matches at or above --threshold produce
GitHub workflow warnings and a Step Summary table. Advisory only — always
exits 0 unless invoked incorrectly.

Usage (from the target repo's checkout, with this package on PYTHONPATH):
  python -m indexer.similar --db code-index.db --repo org/name \
      --base origin/main [--threshold 0.93] [--md similar.md]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import struct
import subprocess
import sys

from . import chunker
from .db import DIMS, MODEL_NAME

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def added_ranges(base: str) -> dict[str, list[tuple[int, int]]]:
    """path -> [(start, end)] of lines added relative to base."""
    out: dict[str, list[tuple[int, int]]] = {}
    current = None
    diff = _git("diff", "--unified=0", "--no-renames", f"{base}...HEAD")
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+++ "):
            current = None  # /dev/null etc.
        elif current and (m := HUNK_RE.match(line)):
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count > 0:
                out.setdefault(current, []).append((start, start + count - 1))
    return out


def changed_chunks(ranges: dict[str, list[tuple[int, int]]]) -> list:
    """(path, Chunk, lang) for chunks overlapping any added range."""
    items = []
    for path, spans in ranges.items():
        ext = os.path.splitext(path)[1].lower()
        lang = chunker.chunk_lang(ext)
        if not lang or not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            if b"\0" in data[:8192]:
                continue
            text = data.decode("utf-8", "replace")
        except OSError:
            continue
        for ch in chunker.chunk_file(text, ext):
            if any(ch.start_line <= e and ch.end_line >= s for s, e in spans):
                items.append((path, ch, lang))
    return items


def open_index(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def nearest_foreign(db: sqlite3.Connection, vec, own_repo: str):
    """Best match outside own_repo: (repo, path, start, end, symbol, cos_sim)."""
    rows = db.execute(
        "SELECT c.repo, c.path, c.start_line, c.end_line, c.symbol, v.distance "
        "FROM (SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? "
        "      ORDER BY distance LIMIT 20) v JOIN chunks c ON c.id = v.rowid "
        "ORDER BY v.distance",
        (struct.pack(f"{DIMS}f", *vec),),
    ).fetchall()
    for repo, path, s, e, sym, dist in rows:
        if repo == own_repo:
            continue
        # vectors are L2-normalised, so dist^2 = 2 - 2*cos
        return repo, path, s, e, sym, 1.0 - (dist * dist) / 2.0
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--repo", required=True, help="this repository, org/name")
    ap.add_argument("--base", required=True, help="ref to diff against")
    ap.add_argument("--threshold", type=float, default=0.93)
    ap.add_argument("--md", default="", help="write a markdown table here")
    args = ap.parse_args(argv)

    items = changed_chunks(added_ranges(args.base))
    print(f"{len(items)} added/modified chunks to check", flush=True)
    findings = []
    if items:
        from fastembed import TextEmbedding
        cache_dir = os.environ.get("FASTEMBED_CACHE") or os.path.expanduser(
            "~/.cache/fastembed")
        model = TextEmbedding(MODEL_NAME, cache_dir=cache_dir)
        texts = [chunker.embed_text(args.repo, path, ch.text)
                 for path, ch, _ in items]
        db = open_index(args.db)
        for (path, ch, _lang), vec in zip(items, model.embed(texts, batch_size=8)):
            hit = nearest_foreign(db, vec, args.repo)
            if hit and hit[5] >= args.threshold:
                findings.append((path, ch, hit))

    lines_md = []
    for path, ch, (repo, fpath, s, e, sym, sim) in findings:
        loc = f"{path}:{ch.start_line}-{ch.end_line}"
        other = f"{repo}/{fpath}:{s}-{e}"
        print(f"::warning file={path},line={ch.start_line},endLine={ch.end_line}"
              f"::似た実装が既にあります: {other}"
              f"{f' ({sym})' if sym else ''} sim {sim:.2f}", flush=True)
        lines_md.append(f"| `{loc}` | `{other}` | {sym or '-'} | {sim:.2f} |")

    if lines_md:
        md = ("### ⚠ 似た実装が既に存在します\n\n"
              "| 追加コード | 既存の類似実装 | symbol | sim |\n|---|---|---|---|\n"
              + "\n".join(lines_md)
              + "\n\n既存側の再利用・共通化を検討してください "
                "(検出: ippoan/code-search-index の意味索引、advisory)。\n")
    else:
        md = ""
    if args.md:
        with open(args.md, "w") as f:
            f.write(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and md:
        with open(summary, "a") as f:
            f.write(md)
    print(f"{len(findings)} similar-code warnings", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
