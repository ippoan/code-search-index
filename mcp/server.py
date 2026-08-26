"""MCP server: semantic code search over the published index.

Downloads the latest index DB from this repo's GitHub release and serves a
single tool, semantic_code_search. Queries are embedded with the same model
that built the index (jinaai/jina-embeddings-v2-base-code).

Run (stdio):  python mcp/server.py
Register:     claude mcp add code-search -- <venv>/bin/python <repo>/mcp/server.py
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import struct
import time
import urllib.request

try:  # mcp >= 2 renamed FastMCP to MCPServer (same tool()/run() surface)
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

ORG = os.environ.get("CODE_INDEX_ORG", "ippoan")
REPO = os.environ.get("CODE_INDEX_REPO", "code-search-index")
RELEASE_TAG = "index"
ASSET = "code-index.db.gz"
CACHE_DIR = os.environ.get(
    "CODE_INDEX_CACHE", os.path.expanduser("~/.cache/code-search-index"))
REFRESH_SECONDS = int(os.environ.get("CODE_INDEX_REFRESH_SECONDS", "21600"))
DIMS = 768
MODEL_NAME = "jinaai/jina-embeddings-v2-base-code"

DUP_ASSET = "dup-pairs.json"

mcp = FastMCP("code-search")

_model = None
_db: sqlite3.Connection | None = None
_last_check = 0.0
_dup_map: dict[str, list] | None = None


def _db_path() -> str:
    return os.path.join(CACHE_DIR, "code-index.db")


def _dup_path() -> str:
    return os.path.join(CACHE_DIR, DUP_ASSET)


def _load_dup_map() -> dict[str, list]:
    """file -> [(other_file, n_chunks, max_sim)] from the duplicate ledger
    published next to the DB (built by indexer/dedup.py in CI)."""
    out: dict[str, list] = {}
    try:
        with open(_dup_path()) as f:
            for p in json.load(f):
                out.setdefault(p["a"], []).append((p["b"], p["n"], p["max_sim"]))
                out.setdefault(p["b"], []).append((p["a"], p["n"], p["max_sim"]))
    except Exception:
        pass  # ledger is optional — search works without warnings
    return out


def _remote_updated_at() -> str | None:
    url = f"https://api.github.com/repos/{ORG}/{REPO}/releases/tags/{RELEASE_TAG}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            rel = json.load(resp)
        for asset in rel.get("assets", []):
            if asset["name"] == ASSET:
                return asset["updated_at"]
    except Exception:
        pass
    return None


def _download() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    url = f"https://github.com/{ORG}/{REPO}/releases/download/{RELEASE_TAG}/{ASSET}"
    tmp_gz = _db_path() + ".gz.tmp"
    tmp_db = _db_path() + ".tmp"
    with urllib.request.urlopen(url, timeout=600) as resp, open(tmp_gz, "wb") as f:
        shutil.copyfileobj(resp, f)
    with gzip.open(tmp_gz, "rb") as src, open(tmp_db, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(tmp_gz)
    os.replace(tmp_db, _db_path())
    # duplicate ledger rides along with the DB; failure is non-fatal
    try:
        dup_url = (f"https://github.com/{ORG}/{REPO}/releases/download/"
                   f"{RELEASE_TAG}/{DUP_ASSET}")
        tmp = _dup_path() + ".tmp"
        with urllib.request.urlopen(dup_url, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, _dup_path())
    except Exception:
        pass


def _ensure_db() -> sqlite3.Connection:
    global _db, _last_check
    now = time.time()
    stamp = os.path.join(CACHE_DIR, "updated_at.txt")
    if _db is None or now - _last_check > REFRESH_SECONDS:
        _last_check = now
        remote = _remote_updated_at()
        local = None
        if os.path.exists(stamp):
            with open(stamp) as f:
                local = f.read().strip()
        if not os.path.exists(_db_path()) or (remote and remote != local):
            if _db is not None:
                _db.close()
                _db = None
            global _dup_map
            _dup_map = None  # reload the ledger with the new DB
            _download()
            if remote:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(stamp, "w") as f:
                    f.write(remote)
    if _db is None:
        # The MCP framework runs tool calls on varying worker threads while we
        # cache one connection globally — the default check_same_thread=True
        # made every call fail permanently once the creating thread was gone.
        # Read-only use on a serialized-threadsafety build, so sharing is safe.
        db = sqlite3.connect(_db_path(), check_same_thread=False)
        db.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        _db = db
    return _db


def _ensure_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    return _model


@mcp.tool()
def semantic_code_search(query: str, k: int = 8, repo: str = "") -> str:
    """Search the ippoan public-repo codebase by meaning, not exact text.

    Use natural language (Japanese or English) to describe the behavior or
    concept you are looking for, e.g. "勤怠の休息時間を丸める処理" or
    "tenant_id resolution from KV". Returns the top matching code chunks as
    repo/path:start-end with a snippet. Optional `repo` restricts results to
    one repository name.
    """
    db = _ensure_db()
    model = _ensure_model()
    vec = next(iter(model.embed([query])))
    fetch = max(k * 8, 50) if repo else k
    rows = db.execute(
        "SELECT c.repo, c.path, c.start_line, c.end_line, c.symbol, c.lang, "
        "c.text, v.distance "
        "FROM (SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? "
        "      ORDER BY distance LIMIT ?) v "
        "JOIN chunks c ON c.id = v.rowid ORDER BY v.distance",
        (struct.pack(f"{DIMS}f", *vec), fetch),
    ).fetchall()
    if repo:
        rows = [r for r in rows if r[0] == repo][:k]
    if not rows:
        return "no results"
    global _dup_map
    if _dup_map is None:
        _dup_map = _load_dup_map()
    out = []
    for r, path, start, end, symbol, lang, text, dist in rows:
        head = f"## {r}/{path}:{start}-{end}"
        if symbol:
            head += f"  ({symbol})"
        block = f"{head}  [dist {dist:.3f}]"
        for other, n, sim in _dup_map.get(f"{r}/{path}", []):
            block += (f"\n⚠ near-duplicate: このファイルは {other} と"
                      f"ほぼ同一の実装を含む (chunks {n}, sim {sim})")
        snippet = "\n".join(text.split("\n")[:25])
        out.append(f"{block}\n```{lang}\n{snippet}\n```")
    updated = db.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
    out.append(f"index updated_at: {updated[0] if updated else 'unknown'}")
    return "\n\n".join(out)


if __name__ == "__main__":
    mcp.run()
