"""Standalone inference for an exported forest — stdlib only.

The trees are exported as flat arrays (`from_lightgbm`) so that whatever does
the injecting (a hook, the MCP server, later a wasm port) can score candidates
without LightGBM, numpy, or a model file format that only LightGBM can read.

Node i of a tree: `f[i] < 0` means a leaf with value `v[i]`; otherwise go to
`l[i]` when `x[f[i]] <= t[i]` and to `r[i]` otherwise.
"""
from __future__ import annotations

import json
import math

FORMAT = "lgbm-forest-v1"


def _flatten(node, tree: dict) -> int:
    """Append node (depth-first) to the flat arrays; returns its index."""
    idx = len(tree["f"])
    if "leaf_value" in node:
        tree["f"].append(-1)
        tree["t"].append(0.0)
        tree["l"].append(-1)
        tree["r"].append(-1)
        tree["v"].append(float(node["leaf_value"]))
        return idx
    if node.get("decision_type", "<=") != "<=":
        raise ValueError(f"unsupported decision_type {node['decision_type']!r}")
    tree["f"].append(int(node["split_feature"]))
    tree["t"].append(float(node["threshold"]))
    tree["l"].append(-1)
    tree["r"].append(-1)
    tree["v"].append(0.0)
    tree["l"][idx] = _flatten(node["left_child"], tree)
    tree["r"][idx] = _flatten(node["right_child"], tree)
    return idx


def from_lightgbm(booster, features: list[str], threshold: float) -> dict:
    """Exportable dict for a binary-objective LightGBM booster.

    Leaf values already include LightGBM's average-init term, so the raw score
    is the plain sum over trees (verified against `predict(raw_score=True)` in
    tests/test_ranker_predict.py).
    """
    dump = booster.dump_model()
    if dump["objective"].split()[0] != "binary":
        raise ValueError(f"unsupported objective {dump['objective']!r}")
    trees = []
    for info in dump["tree_info"]:
        tree = {"f": [], "t": [], "l": [], "r": [], "v": []}
        _flatten(info["tree_structure"], tree)
        trees.append(tree)
    return {"format": FORMAT, "objective": "binary", "features": list(features),
            "threshold": float(threshold), "trees": trees}


class Forest:
    def __init__(self, model: dict):
        if model.get("format") != FORMAT:
            raise ValueError(f"unsupported format {model.get('format')!r}")
        self.features: list[str] = model["features"]
        self.threshold: float = model["threshold"]
        self.trees = [(t["f"], t["t"], t["l"], t["r"], t["v"])
                      for t in model["trees"]]

    @classmethod
    def load(cls, path: str) -> "Forest":
        with open(path) as fh:
            return cls(json.load(fh))

    def raw(self, row) -> float:
        total = 0.0
        for f, t, l, r, v in self.trees:
            i = 0
            while f[i] >= 0:
                i = l[i] if row[f[i]] <= t[i] else r[i]
            total += v[i]
        return total

    def score(self, row) -> float:
        """Probability that this candidate is worth injecting."""
        return 1.0 / (1.0 + math.exp(-self.raw(row)))

    def inject(self, row) -> bool:
        return self.raw(row) >= _logit(self.threshold)


def _logit(p: float) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1.0 - p))
