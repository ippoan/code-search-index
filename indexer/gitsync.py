"""Clone/fetch org repos and compute changed files between commits."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

EXCLUDE_DIR_PARTS = {
    "node_modules", ".git", "target", "dist", "build", "out", "vendor",
    "third_party", ".next", ".nuxt", "coverage", "__pycache__", ".gradle",
    "Pods", ".venv", "venv", "generated",
}
MAX_FILE_BYTES = 300_000


def list_public_repos(org: str) -> list[str]:
    repos, page = [], 1
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/orgs/{org}/repos?type=public&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        with urllib.request.urlopen(req) as resp:
            batch = json.load(resp)
        if not batch:
            break
        for r in batch:
            if r.get("fork") or r.get("archived") or r.get("size", 0) == 0:
                continue
            repos.append(r["name"])
        page += 1
    return sorted(repos)


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def sync_repo(workdir: str, slug: str) -> str:
    """Clone or update the repo (slug = "org/name"); returns HEAD sha of the
    default branch. All repo identifiers are org-qualified slugs so multiple
    orgs can share one index without name collisions."""
    path = os.path.join(workdir, slug)
    url = f"https://github.com/{slug}.git"
    if os.path.isdir(os.path.join(path, ".git")):
        _git(path, "fetch", "--quiet", "origin")
        _git(path, "reset", "--hard", "--quiet", "origin/HEAD")
    else:
        subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", "--single-branch", url, path],
            check=True, capture_output=True, text=True,
        )
    return _git(path, "rev-parse", "HEAD")


def checkout_sha(workdir: str, name: str, sha: str) -> None:
    """Pin the working tree to a specific commit (shard builds share one
    shas file so every shard slices the exact same tree)."""
    path = os.path.join(workdir, name)
    try:
        _git(path, "cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError:
        _git(path, "fetch", "--quiet", "origin", sha)
    _git(path, "reset", "--hard", "--quiet", sha)


def changed_files(workdir: str, name: str, old: str, new: str) -> list[tuple[str, str]] | None:
    """[(status, path)] between old..new, or None if old is unknown (full reindex)."""
    path = os.path.join(workdir, name)
    try:
        _git(path, "cat-file", "-e", f"{old}^{{commit}}")
    except subprocess.CalledProcessError:
        return None
    diff = _git(path, "diff", "--name-status", "--no-renames", old, new)
    out = []
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0][:1], parts[-1]))
    return out


def list_files(workdir: str, name: str) -> list[str]:
    path = os.path.join(workdir, name)
    return _git(path, "ls-files").splitlines()


def wanted(path: str) -> bool:
    parts = path.split("/")
    if any(p in EXCLUDE_DIR_PARTS for p in parts[:-1]):
        return False
    base = parts[-1]
    if ".min." in base or base.endswith(".lock") or base == "package-lock.json":
        return False
    return True


def read_text(workdir: str, name: str, relpath: str) -> str | None:
    full = os.path.join(workdir, name, relpath)
    try:
        if os.path.getsize(full) > MAX_FILE_BYTES:
            return None
        with open(full, "rb") as f:
            head = f.read(8192)
            if b"\0" in head:
                return None
            data = head + f.read()
        return data.decode("utf-8", "replace")
    except OSError:
        return None
