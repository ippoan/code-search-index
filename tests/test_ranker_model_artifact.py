"""The checked-in ranker/model.json must stay loadable without LightGBM."""
import json
import os

from indexer.search import Hit
from ranker.features import FEATURES, group_features
from ranker.predict import Forest

MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ranker", "model.json")


def test_model_matches_the_current_feature_definition():
    with open(MODEL) as f:
        model = json.load(f)
    assert model["features"] == FEATURES, \
        "ranker/model.json was exported from a different feature set — retrain"
    assert 0.0 < model["threshold"] < 1.0
    assert model["trees"]


def test_model_scores_a_real_candidate_list():
    forest = Forest.load(MODEL)
    hits = [
        Hit("org/r", "src/tenant.ts", 1, 40, "resolveTenant", "typescript",
            800, 0.7),
        Hit("org/other", "docs/readme.md", 1, 5, "", "markdown", 200, 1.1),
    ]
    scores = [forest.score(r) for r in
              group_features("resolve tenant from KV", "org/r", hits)]
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores[0] > scores[1]  # own repo, code, name matches the query
    assert forest.inject(group_features("resolve tenant from KV", "org/r",
                                        hits)[0]) is True
