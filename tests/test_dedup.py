import numpy as np

from indexer import dedup


def _unit(v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_find_pairs_cross_repo_only():
    meta = [
        ("org/r1", "src/a.rs", 1, "dup_fn"),
        ("org/r2", "src/b.rs", 1, "dup_fn_copy"),
        ("org/r1", "src/c.rs", 1, "unrelated"),
        ("org/r1", "src/a2.rs", 1, "same_repo_twin"),
    ]
    base = [1.0, 0.0, 0.0, 0.0]
    V = np.vstack([
        _unit(base),
        _unit([0.999, 0.01, 0.0, 0.0]),   # near-identical, other repo -> pair
        _unit([0.0, 1.0, 0.0, 0.0]),      # orthogonal -> no pair
        _unit(base),                       # identical but same repo -> masked
    ])
    pairs = dedup.find_pairs(meta, V, threshold=0.93)
    assert len(pairs) == 1
    p = pairs[0]
    assert {p["a"], p["b"]} == {"org/r1/src/a.rs", "org/r2/src/b.rs"}
    assert p["n"] == 2  # both directions aggregate into the same pair
    assert p["max_sim"] >= 0.99
    assert p["example"] in ("dup_fn", "dup_fn_copy")


def test_find_pairs_below_threshold_is_empty():
    meta = [("org/r1", "a.rs", 1, "f"), ("org/r2", "b.rs", 1, "g")]
    V = np.vstack([_unit([1, 0, 0, 0]), _unit([0.7, 0.7, 0, 0])])
    assert dedup.find_pairs(meta, V, threshold=0.93) == []


def test_render_md_lists_each_pair():
    md = dedup.render_md([
        {"a": "org/r1/a.rs", "b": "org/r2/b.rs", "n": 3,
         "max_sim": 0.97, "example": "verify"},
    ])
    assert "org/r1/a.rs" in md and "org/r2/b.rs" in md
    assert "| 3 | 0.97 |" in md
