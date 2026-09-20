import json

import pytest

from ranker.predict import FORMAT, Forest, from_lightgbm

lgb = pytest.importorskip("lightgbm")  # requirements-ml.txt, not the index job
np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def trained():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(600, 4))
    y = ((X[:, 0] + 0.7 * X[:, 1] + 0.5 * rng.normal(size=600)) > 0).astype(int)
    booster = lgb.train(
        {"objective": "binary", "verbose": -1, "num_leaves": 15,
         "learning_rate": 0.1, "seed": 0},
        lgb.Dataset(X, y), num_boost_round=25)
    return booster, X


def test_exported_forest_matches_lightgbm(trained):
    booster, X = trained
    forest = Forest(from_lightgbm(booster, ["a", "b", "c", "d"], 0.5))
    mine = np.array([forest.raw(row) for row in X.tolist()])
    assert np.abs(mine - booster.predict(X, raw_score=True)).max() < 1e-9
    probs = np.array([forest.score(row) for row in X.tolist()])
    assert np.abs(probs - booster.predict(X)).max() < 1e-9


def test_forest_roundtrips_through_json(tmp_path, trained):
    booster, X = trained
    model = from_lightgbm(booster, ["a", "b", "c", "d"], 0.3)
    assert model["format"] == FORMAT
    path = tmp_path / "m.json"
    path.write_text(json.dumps(model))
    forest = Forest.load(str(path))
    assert forest.features == ["a", "b", "c", "d"]
    assert forest.threshold == 0.3
    row = X[0].tolist()
    assert forest.inject(row) == (forest.score(row) >= 0.3)


def test_threshold_decides_injection(trained):
    booster, X = trained
    rows = X.tolist()
    always = Forest(from_lightgbm(booster, list("abcd"), 1e-9))
    never = Forest(from_lightgbm(booster, list("abcd"), 1 - 1e-9))
    assert all(always.inject(r) for r in rows)
    assert not any(never.inject(r) for r in rows)


def test_rejects_unknown_format():
    with pytest.raises(ValueError):
        Forest({"format": "something-else", "features": [], "threshold": 0.5,
                "trees": []})
