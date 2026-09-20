from indexer.search import Hit, cos_from_distance
from ranker.features import FEATURES, group_features, tokens


def _hit(repo="org/r", path="src/a.rs", cos=0.8, symbol="resolve_tenant",
         lang="rust", text_len=500):
    # cos = 1 - d^2/2  ->  d = sqrt(2 - 2cos)
    dist = (2.0 - 2.0 * cos) ** 0.5
    return Hit(repo, path, 1, 40, symbol, lang, text_len, dist)


def test_tokens_splits_camel_and_snake_and_drops_stopwords():
    t = tokens("fix resolveTenant in auth_worker/src/tenant.ts")
    assert {"resolve", "tenant", "auth", "worker", "src", "ts"} <= t
    assert "fix" not in t and "in" not in t


def test_cos_roundtrip_matches_similar_py_formula():
    assert abs(cos_from_distance(_hit(cos=0.93).distance) - 0.93) < 1e-9


def test_group_features_row_shape_and_relative_columns():
    hits = [_hit(cos=0.9), _hit(path="src/b.rs", cos=0.7),
            _hit(repo="org/other", path="src/c.rs", cos=0.5)]
    rows = group_features("resolve tenant", "org/r", hits)
    assert len(rows) == 3
    assert all(len(r) == len(FEATURES) for r in rows)
    idx = {name: i for i, name in enumerate(FEATURES)}

    assert [r[idx["rank"]] for r in rows] == [0.0, 1.0, 2.0]
    assert rows[0][idx["cos_gap_top"]] == 0.0
    assert rows[1][idx["cos_gap_top"]] > 0.0
    assert all(r[idx["cos_top1"]] == rows[0][idx["cos"]] for r in rows)
    assert [r[idx["same_repo"]] for r in rows] == [1.0, 1.0, 0.0]
    assert rows[0][idx["symbol_overlap"]] > 0.0  # "resolve tenant" vs symbol


def test_group_features_marks_test_paths_and_missing_symbols():
    idx = {name: i for i, name in enumerate(FEATURES)}
    rows = group_features("q", "org/r", [
        _hit(path="src/a.rs"),
        _hit(path="src/tests/a_test.rs", symbol="", lang="markdown"),
    ])
    assert rows[0][idx["is_excluded_path"]] == 0.0
    assert rows[1][idx["is_excluded_path"]] == 1.0
    assert rows[0][idx["is_code_lang"]] == 1.0
    assert rows[1][idx["is_code_lang"]] == 0.0
    assert rows[1][idx["has_symbol"]] == 0.0


def test_group_features_same_file_frac_counts_duplicate_files():
    idx = {name: i for i, name in enumerate(FEATURES)}
    rows = group_features("q", "org/r", [
        _hit(path="src/a.rs"), _hit(path="src/a.rs", cos=0.7),
        _hit(path="src/b.rs", cos=0.6), _hit(path="src/c.rs", cos=0.5),
    ])
    assert rows[0][idx["same_file_frac"]] == 0.5
    assert rows[2][idx["same_file_frac"]] == 0.25


def test_group_features_empty_candidates():
    assert group_features("q", "org/r", []) == []
