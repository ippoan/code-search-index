"""Resolve the shas file for a sharded full build.

Pins every public repo's default-branch HEAD so all N shard jobs slice the
exact same trees. Stdlib only (runs before pip install if needed).

Usage:
  python -m indexer.plan --org ippoan,ohishi-exp --out shas.json
"""
from __future__ import annotations

import argparse
import json
import sys

from .gitsync import get_json, list_public_repos


def _api(path: str):
    return get_json(f"https://api.github.com{path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True,
                    help="comma-separated GitHub orgs")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    shas: dict[str, str] = {}
    for org in args.org.split(","):
        if not org:
            continue
        for name in list_public_repos(org):
            slug = f"{org}/{name}"
            repo = _api(f"/repos/{slug}")
            commit = _api(f"/repos/{slug}/commits/{repo['default_branch']}")
            shas[slug] = commit["sha"]
    with open(args.out, "w") as f:
        json.dump(shas, f, indent=0, sort_keys=True)
    print(f"{len(shas)} repos pinned -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
