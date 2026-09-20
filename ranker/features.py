"""Feature extraction for the injection decision.

One row per (query, candidate chunk). Everything here is derived from what
`indexer.search.search()` already returns plus the query string — no extra
embedding, no extra DB round-trip — so the same code runs at training time
and inside the hook that would do the injecting.

Pure Python and stdlib only: `ranker.predict` evaluates the exported trees
without numpy, and these features have to be computable in the same place.
"""
from __future__ import annotations

import math
import re

from indexer.dedup import CODE_LANGS, EXCLUDE_RE

# Order is the contract between training, export and inference. Appending is
# fine; reordering or removing invalidates an exported model.
FEATURES = [
    "cos",               # cosine similarity of the chunk to the query
    "distance",          # raw L2 distance (what the vec index returns)
    "rank",              # 0-based position in the search result
    "cos_gap_top",       # cos(top1) - cos(this)
    "cos_z",             # (cos - mean) / std within this result list
    "cos_top1",          # how good the best hit of this query was at all
    "same_repo",         # candidate is in the repo being worked on
    "path_depth",        # number of path segments
    "path_overlap",      # query tokens found in the path
    "symbol_overlap",    # query tokens found in the symbol name
    "has_symbol",        # chunker gave this chunk a name
    "log_text_len",      # log1p(chunk length in bytes)
    "is_code_lang",      # indexer.dedup.CODE_LANGS
    "is_excluded_path",  # test/vendored/docs path (indexer.dedup.EXCLUDE_RE)
    "same_file_frac",    # share of the result list coming from this file
]

_SPLIT_RE = re.compile(r"[^0-9A-Za-z぀-ヿ一-鿿]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOP = {"the", "a", "an", "of", "to", "for", "and", "in", "on", "fix", "add",
         "update", "use", "make", "wip"}


def tokens(text: str) -> set[str]:
    """Lowercased identifier-ish tokens; camelCase and snake_case are split."""
    out: set[str] = set()
    for raw in _SPLIT_RE.split(text or ""):
        if not raw:
            continue
        for part in _CAMEL_RE.split(raw):
            t = part.lower()
            if len(t) > 1 and t not in _STOP:
                out.add(t)
    return out


def _overlap(qt: set[str], text: str) -> float:
    ct = tokens(text)
    if not qt or not ct:
        return 0.0
    return len(qt & ct) / len(ct)


def group_features(query: str, query_repo: str, candidates) -> list[list[float]]:
    """Feature rows for one query's candidate list (search order, nearest first).

    `candidates` are `indexer.search.Hit`s or anything with the same
    attributes. `query_repo` is the repo the work is happening in ("org/name",
    "" if unknown).
    """
    cands = list(candidates)
    if not cands:
        return []
    qt = tokens(query)
    coss = [c.cos for c in cands]
    top1 = coss[0]
    mean = sum(coss) / len(coss)
    var = sum((c - mean) ** 2 for c in coss) / len(coss)
    std = math.sqrt(var)
    counts: dict[str, int] = {}
    for c in cands:
        counts[c.file] = counts.get(c.file, 0) + 1

    rows = []
    for rank, c in enumerate(cands):
        rows.append([
            c.cos,
            c.distance,
            float(rank),
            top1 - c.cos,
            (c.cos - mean) / std if std > 1e-9 else 0.0,
            top1,
            1.0 if query_repo and c.repo == query_repo else 0.0,
            float(c.path.count("/") + 1),
            _overlap(qt, c.path),
            _overlap(qt, c.symbol or ""),
            1.0 if c.symbol else 0.0,
            math.log1p(c.text_len),
            1.0 if c.lang in CODE_LANGS else 0.0,
            1.0 if EXCLUDE_RE.search(c.file) else 0.0,
            counts[c.file] / len(cands),
        ])
    return rows
