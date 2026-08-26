"""Cross-repo duplicate-code report from the index embeddings.

Mines pairs of files in *different* repos whose chunks are near-identical in
embedding space (cosine >= SIM_THRESHOLD). Tests, vendored trees, docs and
generated files are excluded; only substantial named chunks count.

Run after every index update. With --baseline (the previous report), only
pairs absent from the baseline are written to --new-out / --new-md — the
signal for "a fresh duplication just got merged". Without a baseline the run
just establishes one (no new-pair output), so the first run never spams.

Usage:
  python -m indexer.dedup --db code-index.db --out dup-pairs.json \
      [--baseline baseline.json --new-out new-pairs.json --new-md new-pairs.md]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

CODE_LANGS = {"rust", "typescript", "tsx", "javascript", "python", "go",
              "kotlin", "java", "c", "cpp", "vue"}
EXCLUDE_RE = re.compile(
    r"(alc-gw-p4/components/|/tests?/|(^|/)test_|\.test\.|_test\.|\.spec\."
    r"|\.d\.ts$|/docs/|/fixtures/|/examples?/|setup-bazel)")
MIN_TEXT_LEN = 300
SIM_THRESHOLD = 0.93
BLOCK = 2000


def load_analysis_set(db_path: str):
    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    import numpy as np
    meta, vecs = [], []
    for repo, path, sl, sym, lang, tlen, emb in db.execute(
        "SELECT c.repo, c.path, c.start_line, c.symbol, c.lang, length(c.text), "
        "v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id"
    ):
        if lang not in CODE_LANGS or tlen < MIN_TEXT_LEN or not sym:
            continue
        if EXCLUDE_RE.search(f"{repo}/{path}"):
            continue
        meta.append((repo, path, sl, sym))
        vecs.append(np.frombuffer(emb, dtype=np.float32))
    db.close()
    if not meta:
        return meta, None
    V = np.vstack(vecs)
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    return meta, V


def find_pairs(meta, V, threshold: float = SIM_THRESHOLD) -> list[dict]:
    """Best cross-repo match per chunk, aggregated to file pairs."""
    import numpy as np
    repos = np.array([m[0] for m in meta])
    n = len(meta)
    pairs: dict[tuple, dict] = {}
    for s in range(0, n, BLOCK):
        e = min(s + BLOCK, n)
        sims = V[s:e] @ V.T
        sims[repos[s:e, None] == repos[None, :]] = -1  # mask same-repo
        j = sims.argmax(axis=1)
        best = sims[np.arange(e - s), j]
        for k in range(e - s):
            if best[k] < threshold:
                continue
            i, jj = s + k, int(j[k])
            a = f"{meta[i][0]}/{meta[i][1]}"
            b = f"{meta[jj][0]}/{meta[jj][1]}"
            key = tuple(sorted((a, b)))
            d = pairs.setdefault(key, {"a": key[0], "b": key[1], "n": 0,
                                       "max_sim": 0.0, "example": ""})
            d["n"] += 1
            if float(best[k]) > d["max_sim"]:
                d["max_sim"] = round(float(best[k]), 3)
                d["example"] = meta[i][3] or meta[jj][3]
    return sorted(pairs.values(), key=lambda p: (-p["n"], -p["max_sim"]))


def pair_key(p: dict) -> tuple:
    return (p["a"], p["b"])


def render_md(new_pairs: list[dict]) -> str:
    lines = [
        "索引更新で **新しい repo 横断のコード重複** を検出しました。",
        "前回のレポート (Release `index` の `dup-pairs.json`) に無かったペアのみ:",
        "",
        "| chunks | max sim | file A | file B | 例 |",
        "|---|---|---|---|---|",
    ]
    for p in new_pairs:
        lines.append(
            f"| {p['n']} | {p['max_sim']} | `{p['a']}` | `{p['b']}` "
            f"| `{p['example']}` |")
    lines += ["", "---", "検出: indexer/dedup.py (cos >= "
              f"{SIM_THRESHOLD}、テスト/vendored/生成物は除外)。"
              "既知のペアの台帳は Release asset `dup-pairs.json`。"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", default="")
    ap.add_argument("--new-out", default="")
    ap.add_argument("--new-md", default="")
    args = ap.parse_args(argv)

    meta, V = load_analysis_set(args.db)
    pairs = find_pairs(meta, V) if V is not None else []
    with open(args.out, "w") as f:
        json.dump(pairs, f, ensure_ascii=False, indent=1)
    print(f"{len(meta)} chunks analysed, {len(pairs)} duplicate file pairs",
          flush=True)

    new_pairs: list[dict] = []
    if args.baseline and os.path.exists(args.baseline):
        try:
            with open(args.baseline) as f:
                known = {pair_key(p) for p in json.load(f)}
            new_pairs = [p for p in pairs if pair_key(p) not in known]
            print(f"{len(new_pairs)} new pairs vs baseline", flush=True)
        except Exception as e:
            print(f"baseline unreadable ({e}) -> treating as first run",
                  file=sys.stderr)
    else:
        print("no baseline -> establishing one (no new-pair output)", flush=True)

    if args.new_out:
        with open(args.new_out, "w") as f:
            json.dump(new_pairs, f, ensure_ascii=False, indent=1)
    if args.new_md:
        with open(args.new_md, "w") as f:
            f.write(render_md(new_pairs) if new_pairs else "")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Duplicate-code report\n\n{len(pairs)} known pairs, "
                    f"{len(new_pairs)} new this run\n\n")
            if new_pairs:
                f.write(render_md(new_pairs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
