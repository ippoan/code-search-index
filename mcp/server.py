"""MCP server: semantic code search and call-graph lookup over the published index.

Downloads the published assets from this repo's GitHub release and serves two
tools: semantic_code_search (vector search over code chunks) and find_callers
(who calls this, from calls.db). Queries are embedded with the same model that
built the index (jinaai/jina-embeddings-v2-base-code).

Run (stdio):  python mcp/server.py
Register:     claude mcp add code-search -- <venv>/bin/python <repo>/mcp/server.py
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
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
CALLS_ASSET = "calls.db.gz"

# This server runs as `python mcp/server.py`, so it cannot import `indexer`:
# putting the repo root on sys.path would shadow the installed `mcp` SDK with
# this repo's `mcp/` directory. It therefore keeps its own copy of the call
# lookup, as indexer/search.py records for the vector SQL.
# --- calls SQL + helpers (identical copy in mcp/server.py / indexer/calls.py —
#     tests/test_calls.py compares the two blocks as text) ---
SQL_TARGETS_BY_SYMBOL = """
SELECT id, repo, name, kind, path, start_line, end_line
  FROM symbols
 WHERE name = :symbol
   AND (:repo = '' OR repo = :repo)
 ORDER BY repo, path, start_line
 LIMIT :limit
"""

# A line range also lands inside the module/class that spans the file, whose
# references are every `use` of it — noise that crowds out the answer. When a
# range is given, keep only the innermost matches (kind names differ per
# indexer, so this goes by span, not by kind).
SQL_TARGETS_BY_PATH = """
SELECT id, repo, name, kind, path, start_line, end_line
  FROM symbols s
 WHERE path = :path
   AND (:repo = '' OR repo = :repo)
   AND (:line_to = 0 OR (start_line <= :line_to AND end_line >= :line_from))
   AND (:line_to = 0 OR NOT EXISTS (
         SELECT 1 FROM symbols i
          WHERE i.repo = s.repo AND i.path = s.path
            AND i.start_line <= :line_to AND i.end_line >= :line_from
            AND i.start_line >= s.start_line AND i.end_line <= s.end_line
            AND i.end_line - i.start_line < s.end_line - s.start_line))
 ORDER BY repo, start_line
 LIMIT :limit
"""

# {ids} is filled with one named placeholder per target id.
SQL_CALLERS = """
SELECT r.repo, r.path, r.line, r.role, t.name,
       e.name, e.kind, e.path, e.start_line, e.end_line
  FROM refs r
  JOIN symbols t ON t.id = r.symbol_id
  LEFT JOIN symbols e ON e.id = r.enclosing_symbol_id
 WHERE r.symbol_id IN ({ids})
 ORDER BY r.repo, r.path, r.line
 LIMIT :limit
"""

# A call through a trait/interface resolves to the *implemented* member, not to
# the concrete impl, so the impl itself has no references. These rows join the
# two: symbol_id = the implemented member, enclosing_symbol_id = the impl.
SQL_IMPL_TARGETS = """
SELECT DISTINCT s.id, s.repo, s.name, s.kind, s.path, s.start_line, s.end_line
  FROM refs r
  JOIN symbols s ON s.id = r.symbol_id
 WHERE r.enclosing_symbol_id IN ({ids})
   AND r.role = 'implementation'
 ORDER BY s.repo, s.path, s.start_line
 LIMIT :limit
"""

SQL_META = "SELECT key, value FROM meta"

SQL_REPOS = "SELECT repo, commit_sha, indexed_at FROM repos ORDER BY repo"


def _by_ids(sql: str, ids, limit: int) -> tuple[str, dict]:
    """Bind an id list into one of the {ids} templates above."""
    keys = [f"id{i}" for i in range(len(ids))]
    params: dict[str, object] = {key: i for key, i in zip(keys, ids)}
    params["limit"] = limit
    return sql.format(ids=", ".join(":" + key for key in keys)), params
# --- end calls SQL ---

mcp = FastMCP("code-search")

_model = None
_db: sqlite3.Connection | None = None
_calls_db: sqlite3.Connection | None = None
_last_check = 0.0
_dup_map: dict[str, list] | None = None


def _cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _db_path() -> str:
    return _cache_path("code-index.db")


def _dup_path() -> str:
    return _cache_path(DUP_ASSET)


def _calls_path() -> str:
    return _cache_path("calls.db")


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


def _release() -> dict | None:
    """The `index` release as the API reports it; its assets carry `digest`.
    None when unreachable — we then keep serving the cached copies rather than
    downloading something we cannot verify (scripts/sync-db.sh does the same)."""
    url = f"https://api.github.com/repos/{ORG}/{REPO}/releases/tags/{RELEASE_TAG}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except Exception:
        return None


def _sync_asset(rel: dict, name: str, dest: str, *, stamp: str,
                gunzip: bool = False, timeout: int = 600) -> bool:
    """Fetch one release asset into `dest`, verifying the sha256 the Release
    API publishes as the asset's `digest` (the same guarantee as
    scripts/sync-db.sh:33-55). The download is skipped while the cached stamp
    still matches. Returns True when `dest` was rewritten; raises on a digest
    mismatch, leaving the previous copy untouched.
    """
    meta = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
    if meta is None:
        return False  # not published (yet) — callers decide whether that is fatal
    digest = meta.get("digest") or ""  # "sha256:<hex>"; "" on pre-digest releases
    want = digest or meta.get("updated_at") or ""
    stamp_path = _cache_path(stamp)
    if want and os.path.exists(dest):
        try:
            with open(stamp_path) as f:
                if f.read().strip() == want:
                    return False
        except OSError:
            pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    url = f"https://github.com/{ORG}/{REPO}/releases/download/{RELEASE_TAG}/{name}"
    tmp = dest + ".tmp"
    sha = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
        for block in iter(lambda: resp.read(1 << 20), b""):
            sha.update(block)
            f.write(block)
    got = "sha256:" + sha.hexdigest()
    if digest and got != digest:
        os.remove(tmp)
        raise RuntimeError(
            f"checksum mismatch: expected {digest} got {got} — 前回のファイルを"
            "維持します (アップロード途中か改竄の可能性)")
    if gunzip:
        raw = dest + ".raw.tmp"
        with gzip.open(tmp, "rb") as src, open(raw, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(tmp)
        tmp = raw
    os.replace(tmp, dest)
    if want:
        with open(stamp_path, "w") as f:
            f.write(want)
    return True


def _refresh() -> None:
    """Pull whatever changed in the release. Each asset is independent: a
    failure warns and keeps the previous copy, and a missing asset just means
    the tool that needs it says so."""
    global _db, _calls_db, _dup_map
    rel = _release()
    if rel is None:
        return
    for name, dest, stamp, gz in (
            (ASSET, _db_path(), "db-digest.txt", True),
            (DUP_ASSET, _dup_path(), "dup-digest.txt", False),
            (CALLS_ASSET, _calls_path(), "calls-digest.txt", True)):
        try:
            if not _sync_asset(rel, name, dest, stamp=stamp, gunzip=gz):
                continue
        except Exception as e:
            print(f"⚠ [code-search] {name}: {e}", file=sys.stderr)
            continue
        if name == ASSET and _db is not None:
            _db.close()
            _db = None
        elif name == DUP_ASSET:
            _dup_map = None  # reload the ledger alongside the new DB
        elif name == CALLS_ASSET and _calls_db is not None:
            _calls_db.close()
            _calls_db = None


def _refresh_due(force: bool = False) -> None:
    global _last_check
    now = time.time()
    if force or now - _last_check > REFRESH_SECONDS:
        _last_check = now
        _refresh()


def _connect(path: str) -> sqlite3.Connection:
    # The MCP framework runs tool calls on varying worker threads while we
    # cache one connection globally — the default check_same_thread=True
    # made every call fail permanently once the creating thread was gone.
    # Read-only use on a serialized-threadsafety build, so sharing is safe.
    return sqlite3.connect(path, check_same_thread=False)


def _ensure_db() -> sqlite3.Connection:
    global _db
    # a missing index retries every call rather than waiting out the window
    _refresh_due(force=not os.path.exists(_db_path()))
    if _db is None:
        if not os.path.exists(_db_path()):
            raise RuntimeError(
                f"検索索引がまだ手元にありません ({ASSET} を取得できませんでした)")
        db = _connect(_db_path())
        db.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        _db = db
    return _db


def _ensure_calls_db() -> sqlite3.Connection | None:
    """calls.db comes from a separate extraction job — None until it first
    publishes, and find_callers says so instead of failing."""
    global _calls_db
    _refresh_due()
    if _calls_db is None and os.path.exists(_calls_path()):
        _calls_db = _connect(_calls_path())
    return _calls_db


def _ensure_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    return _model


def _parse_lines(lines: str) -> tuple[int, int]:
    """"120" -> (120, 120), "120-140" -> (120, 140), "" -> (0, 0) = whole file."""
    text = (lines or "").strip()
    if not text:
        return (0, 0)
    first, _, last = text.partition("-")
    try:
        lo, hi = int(first), int(last or first)
    except ValueError:
        raise ValueError(f"lines: expected N or N-M, got {lines!r}") from None
    if lo > hi:
        lo, hi = hi, lo
    return (max(lo, 1), max(hi, 1))


def _freshness(db: sqlite3.Connection, repos: set[str]) -> str:
    meta = dict(db.execute(SQL_META).fetchall())
    rows = [r for r in db.execute(SQL_REPOS).fetchall()
            if not repos or r[0] in repos]
    head = f"calls.db updated_at {meta.get('updated_at') or 'unknown'}"
    if meta.get("generator"):
        head += f" (generator {meta['generator']})"
    parts = [f"{repo} @ {(sha or '?')[:12]} ({at or '?'})" for repo, sha, at in rows]
    return head + ("; " + ", ".join(parts) if parts else "")


@mcp.tool()
def find_callers(symbol: str = "", repo: str = "", path: str = "",
                 lines: str = "", k: int = 30) -> str:
    """Who calls this? Call sites across the indexed public repos.

    grep misses calls made through traits/interfaces or a router; this reads a
    SCIP-derived call graph instead, following one hop from a concrete impl to
    the member it implements. Pass `symbol` (a function/method/type name) or
    `path` (repo-relative, plus `lines` like "40-80" to mean "the definitions
    living there") to see the blast radius before changing code.
    Optional `repo` is "org/name". Answers carry the commit each repo was
    extracted at, so staleness is visible.
    """
    if not symbol and not path:
        return "find_callers: symbol か path のどちらかを指定してください"
    db = _ensure_calls_db()
    if db is None:
        return (f"呼び出し関係の索引がまだありません (Release `{RELEASE_TAG}` の "
                f"{CALLS_ASSET} が未公開か未取得です)。grep で代替してください。")
    try:
        if symbol:
            query = f"symbol={symbol}"
            rows = db.execute(SQL_TARGETS_BY_SYMBOL, {
                "symbol": symbol, "repo": repo, "limit": max(k, 100)}).fetchall()
        else:
            query = f"path={path}" + (f":{lines}" if lines else "")
            line_from, line_to = _parse_lines(lines)
            rows = db.execute(SQL_TARGETS_BY_PATH, {
                "path": path, "repo": repo, "line_from": line_from,
                "line_to": line_to, "limit": max(k, 100)}).fetchall()
        if repo:
            query += f" repo={repo}"
        if not rows:
            return (f"{query}: calls.db に一致する定義がありません "
                    f"(未索引の repo / 名前違い / 索引が古い)\n{_freshness(db, set())}")
        # a call through a trait/interface resolves to the implemented member,
        # so the concrete impl has no references of its own — follow one hop
        impl_sql, impl_params = _by_ids(SQL_IMPL_TARGETS, [r[0] for r in rows], 100)
        known = {r[0] for r in rows}
        extra = [r for r in db.execute(impl_sql, impl_params).fetchall()
                 if r[0] not in known]
        via = {r[0] for r in extra}
        rows = rows + extra
        touched = {r[1] for r in rows}
        sql, params = _by_ids(SQL_CALLERS, [r[0] for r in rows], k + 1)
        refs = db.execute(sql, params).fetchall()
    except ValueError as e:
        return f"find_callers: {e}"
    except sqlite3.DatabaseError as e:
        return f"calls.db を読めません: {e}"

    callers, seen = [], set()
    for r_repo, r_path, line, role, target, name, kind, *_rest in refs:
        key = (r_repo, r_path, line, name)
        if key in seen:  # one line referencing several matched defs counts once
            continue
        seen.add(key)
        touched.add(r_repo)
        callers.append(f"- {r_repo}/{r_path}:{line}  in {name or '(file scope)'}"
                       f"{' (' + kind + ')' if kind else ''}"
                       f"  [{role or 'reference'} -> {target}]")
    truncated = len(callers) > k
    callers = callers[:k]

    out = [f"# 呼び出し元 {len(callers)}{'+' if truncated else ''} 件 — {query}",
           "## 対象の定義"]
    out += [f"- {r[1]}/{r[4]}:{r[5]} {r[2]} ({r[3] or 'symbol'})"
            + ("  ← 実装元 (trait/interface 越しの呼び出しはここに解決される)"
               if r[0] in via else "")
            for r in rows[:10]]
    if len(rows) > 10:
        out.append(f"- … ほか {len(rows) - 10} 件")
    out.append("## 呼び出し元")
    out += callers or ["- 0 件 (この索引の範囲では参照されていません)"]
    if truncated:
        out.append("- … 打ち切りました (k を上げるとさらに表示されます)")
    out.append(f"\n{_freshness(db, touched)}")
    return "\n".join(out)


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
