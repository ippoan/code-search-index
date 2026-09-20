"""Train and evaluate the injection decision, then export the trees.

Evaluation is a *time-based* hold-out: the split is a commit-date quantile, so
every test query is newer than every training query. Baselines, all computed
on the same test queries and all tuned on the training split only:

  cos            a flat cosine cut-off — the shape of rule `indexer.similar`
                 uses (its fixed 0.93 default is reported too).
  repo           inject everything from the repo being worked on.
  repo+cos       same repo AND cosine above a cut-off. This is the strong
                 baseline: the labels are mined from single-repo commits, so
                 "it is in my repo" is most of the signal, and any honest
                 comparison has to hand that to the threshold rule as well.

For ranking (NDCG@k, P@k) the baselines are the orders those rules imply:
plain cosine (= the order the index already returns) and same-repo-first.

Usage:
  python -m ranker.train --data dataset.jsonl --out-model model.json \
      --out-report report.json [--split 0.8] [--drop-features same_repo]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

from indexer.search import Hit
from .features import FEATURES, group_features
from .predict import Forest, from_lightgbm

SIMILAR_PY_THRESHOLD = 0.93  # indexer/similar.py --threshold default
TOPK_REPORTED = (5, 10)
COS = FEATURES.index("cos")
SAME_REPO = FEATURES.index("same_repo")


def load_groups(path: str, require_positive: bool = True) -> list[dict]:
    groups = []
    with open(path) as f:
        for line in f:
            g = json.loads(line)
            if require_positive and not g.get("n_pos"):
                continue
            groups.append(g)
    groups.sort(key=lambda g: g["ts"])
    return groups


def build(groups) -> tuple[list[list[list[float]]], list[list[int]]]:
    """Per-group feature rows and labels, in search order."""
    grows, glabels = [], []
    for g in groups:
        hits = [Hit(c["repo"], c["path"], c["start_line"], c["end_line"],
                    c["symbol"], c["lang"], c["text_len"], c["distance"])
                for c in g["candidates"]]
        grows.append(group_features(g["query"], g["repo"], hits))
        glabels.append([c["label"] for c in g["candidates"]])
    return grows, glabels


def flat(nested):
    return [x for group in nested for x in group]


def prf(y_true, flags) -> dict:
    tp = sum(1 for t, p in zip(y_true, flags) if t and p)
    fp = sum(1 for t, p in zip(y_true, flags) if not t and p)
    fn = sum(1 for t, p in zip(y_true, flags) if t and not p)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "selected": tp + fp}


def best_threshold(scores, y_true) -> tuple[float, float]:
    """Threshold maximising F1 on these scores; returns (threshold, f1)."""
    best, best_f1 = 0.5, -1.0
    for cut in sorted({round(s, 4) for s in scores}):
        f1 = prf(y_true, [s >= cut for s in scores])["f1"]
        if f1 > best_f1:
            best, best_f1 = cut, f1
    return best, best_f1


def rank_metrics(glabels, gscores) -> dict:
    """NDCG@k and P@k, averaged over queries."""
    from sklearn.metrics import ndcg_score
    out = {}
    for k in TOPK_REPORTED:
        ndcgs, precs = [], []
        for labels, scores in zip(glabels, gscores):
            if len(labels) < 2:
                continue
            ndcgs.append(ndcg_score([labels], [scores], k=k))
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
            precs.append(sum(labels[i] for i in order) / len(order))
        out[f"ndcg@{k}"] = round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None
        out[f"p@{k}"] = round(sum(precs) / len(precs), 4) if precs else None
    return out


def score_groups(grows, fn) -> list[list[float]]:
    return [[fn(r) for r in rows] for rows in grows]


def evaluate(train_scores, y_train, test_scores, y_test, test_labels,
             test_gscores) -> dict:
    """Tune the cut-off on train, report everything on test."""
    from sklearn.metrics import average_precision_score
    thr, _ = best_threshold(train_scores, y_train)
    return {
        "threshold": thr,
        **prf(y_test, [s >= thr for s in test_scores]),
        "pr_auc": round(float(average_precision_score(y_test, test_scores)), 4),
        **rank_metrics(test_labels, test_gscores),
    }


def conditions(groups, label: str) -> dict:
    ts = [g["ts"] for g in groups]
    rows = sum(len(g["candidates"]) for g in groups)
    pos = sum(c["label"] for g in groups for c in g["candidates"])
    return {
        "split": label,
        "queries": len(groups),
        "repos": len({g["repo"] for g in groups}),
        "candidates": rows,
        "positives": pos,
        "positive_rate": round(pos / rows, 4) if rows else 0.0,
        "first_commit": dt.datetime.fromtimestamp(min(ts), dt.UTC).date().isoformat(),
        "last_commit": dt.datetime.fromtimestamp(max(ts), dt.UTC).date().isoformat(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out-model", required=True)
    ap.add_argument("--out-report", default="")
    ap.add_argument("--split", type=float, default=0.8,
                    help="commit-date quantile; test is everything newer")
    ap.add_argument("--rounds", type=int, default=400)
    ap.add_argument("--drop-features", default="",
                    help="comma-separated feature names to exclude (ablation)")
    ap.add_argument("--keep-negative-groups", action="store_true",
                    help="also train on queries with no positive in top-k")
    args = ap.parse_args(argv)

    import lightgbm as lgb
    import numpy as np

    dropped = {f for f in args.drop_features.split(",") if f}
    unknown = dropped - set(FEATURES)
    if unknown:
        print(f"unknown feature(s): {sorted(unknown)}", file=sys.stderr)
        return 2
    keep = [i for i, f in enumerate(FEATURES) if f not in dropped]
    used = [FEATURES[i] for i in keep]

    groups = load_groups(args.data, require_positive=not args.keep_negative_groups)
    if len(groups) < 50:
        print(f"only {len(groups)} usable queries", file=sys.stderr)
        return 1
    cut = int(len(groups) * args.split)
    train_g, test_g = groups[:cut], groups[cut:]
    # last 15% of train (still older than every test query) is the valid slice
    vcut = int(len(train_g) * 0.85)

    train_rows, train_labels = build(train_g)
    test_rows, test_labels = build(test_g)
    ytr, yt = flat(train_labels), flat(test_labels)
    Xtr, Xt = flat(train_rows), flat(test_rows)
    Xf, yf = flat(train_rows[:vcut]), flat(train_labels[:vcut])
    Xv, yv = flat(train_rows[vcut:]), flat(train_labels[vcut:])

    def sel(X):
        return np.array([[r[i] for i in keep] for r in X])

    t0 = time.perf_counter()
    booster = lgb.train(
        {"objective": "binary", "metric": "average_precision",
         "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 40,
         "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1,
         "verbose": -1, "seed": 0},
        lgb.Dataset(sel(Xf), np.array(yf), feature_name=used),
        num_boost_round=args.rounds,
        valid_sets=[lgb.Dataset(sel(Xv), np.array(yv))],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    train_s = time.perf_counter() - t0

    # operating point comes from the training split only
    train_scores = booster.predict(sel(Xtr)).tolist()
    thr, train_f1 = best_threshold(train_scores, ytr)
    model = from_lightgbm(booster, used, thr)
    with open(args.out_model, "w") as f:
        json.dump(model, f)
    forest = Forest(model)

    rows_t = [[r[i] for i in keep] for r in Xt]
    test_scores = booster.predict(np.array(rows_t)).tolist()
    t0 = time.perf_counter()
    test_raw = [forest.raw(r) for r in rows_t]
    infer_s = time.perf_counter() - t0
    drift = max(abs(1 / (1 + np.exp(-r)) - s)
                for r, s in zip(test_raw, test_scores))

    model_g = score_groups(test_rows, lambda r: forest.raw([r[i] for i in keep]))
    cos_only = (lambda r: r[COS])
    repo_first = (lambda r: r[COS] + r[SAME_REPO])      # same repo sorts first
    repo_and_cos = (lambda r: r[COS] if r[SAME_REPO] else -1.0)

    k_per_query = round(len(Xt) / len(test_g), 1)
    report = {
        "conditions": {
            "dataset": os.path.basename(args.data),  # local paths stay local
            "label_rule": "chunk's file was changed by the commit "
                          "(file-level, no line overlap)",
            "query_rule": "commit subject, merges/bumps/reverts dropped, "
                          "<=10 indexed files touched",
            "candidates_per_query": k_per_query,
            "split_rule": f"commit-date quantile {args.split} (test is newer)",
            "trained_on_queries_with_positive_only": not args.keep_negative_groups,
            "features_dropped": sorted(dropped),
            "train": conditions(train_g, "train"),
            "test": conditions(test_g, "test"),
        },
        "model": {
            "features": used,
            "trees": len(model["trees"]),
            "best_iteration": booster.best_iteration,
            "train_seconds": round(train_s, 2),
            "threshold": thr,
            "train_f1_at_threshold": round(train_f1, 4),
            "export_bytes": len(json.dumps(model)),
            "lightgbm_vs_exported_max_abs_diff": float(drift),
        },
        "timing": {
            "candidates_scored": len(Xt),
            "us_per_candidate": round(infer_s / len(Xt) * 1e6, 1),
            "ms_per_query": round(infer_s / len(test_g) * 1e3, 3),
            "note": f"pure-Python ranker.predict.Forest, one thread, "
                    f"{k_per_query} candidates per query",
        },
        "test": {
            "model": evaluate(train_scores, ytr, test_scores, yt, test_labels,
                              model_g),
            "baseline_cos": evaluate(
                [cos_only(r) for r in Xtr], ytr, [cos_only(r) for r in Xt], yt,
                test_labels, score_groups(test_rows, cos_only)),
            "baseline_repo": {
                **prf(yt, [r[SAME_REPO] > 0 for r in Xt]),
                **rank_metrics(test_labels,
                               score_groups(test_rows, lambda r: r[SAME_REPO])),
            },
            "baseline_repo_and_cos": evaluate(
                [repo_and_cos(r) for r in Xtr], ytr,
                [repo_and_cos(r) for r in Xt], yt, test_labels,
                score_groups(test_rows, repo_first)),
            "baseline_cos_similar_py": {
                "threshold": SIMILAR_PY_THRESHOLD,
                **prf(yt, [r[COS] >= SIMILAR_PY_THRESHOLD for r in Xt]),
            },
        },
        "feature_gain": dict(sorted(
            zip(used, (round(float(v), 1) for v in
                       booster.feature_importance("gain"))),
            key=lambda kv: -kv[1])),
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text, flush=True)
    if args.out_report:
        with open(args.out_report, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
