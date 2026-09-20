"""Call-graph queries over calls.db — "who calls this?".

calls.db is published next to the search index (Release `index`, asset
`calls.db.gz`) by the SCIP extraction job; this module is the read side. Every
answer carries the freshness of the tree it was extracted from
(`repos.commit_sha` + `meta.updated_at`) so a stale answer can be spotted
without asking.

stdlib + sqlite3 only, on purpose: `mcp/server.py` cannot import this package
(it runs as `python mcp/server.py`, and putting the repo root on sys.path would
shadow the installed `mcp` SDK with this repo's `mcp/` directory — the same
decision recorded in indexer/search.py). The server therefore keeps its own
copy of the SQL block below, and tests/test_calls.py compares the two blocks as
text so they cannot drift apart.

Usage:
  python -m indexer.calls --symbol resolve_tenant [--repo ippoan/auth-worker]
  python -m indexer.calls --path src/router.rs --lines 40-80
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass

# --- calls SQL (identical copy in mcp/server.py / indexer/calls.py —
#     tests/test_calls.py compares the two blocks as text) ---
SQL_TARGETS_BY_SYMBOL = """
SELECT id, repo, name, kind, path, start_line, end_line
  FROM symbols
 WHERE name = :symbol
   AND (:repo = '' OR repo = :repo)
 ORDER BY repo, path, start_line
 LIMIT :limit
"""

SQL_TARGETS_BY_PATH = """
SELECT id, repo, name, kind, path, start_line, end_line
  FROM symbols
 WHERE path = :path
   AND (:repo = '' OR repo = :repo)
   AND (:line_to = 0 OR (start_line <= :line_to AND end_line >= :line_from))
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

SQL_META = "SELECT key, value FROM meta"

SQL_REPOS = "SELECT repo, commit_sha, indexed_at FROM repos ORDER BY repo"
# --- end calls SQL ---

DB_NAME = "calls.db"
CACHE_DIR = os.environ.get(
    "CODE_INDEX_CACHE", os.path.expanduser("~/.cache/code-search-index"))


def default_db_path() -> str:
    return os.path.join(CACHE_DIR, DB_NAME)


@dataclass(frozen=True)
class Target:
    """A definition the query resolved to (what the callers call)."""
    id: int
    repo: str
    name: str
    kind: str
    path: str
    start_line: int
    end_line: int

    @property
    def location(self) -> str:
        return f"{self.repo}/{self.path}:{self.start_line}"


@dataclass(frozen=True)
class Caller:
    """One reference site, plus the definition that encloses it."""
    repo: str
    path: str
    line: int
    role: str
    target_name: str
    name: str
    kind: str
    def_path: str
    def_start_line: int
    def_end_line: int

    @property
    def location(self) -> str:
        return f"{self.repo}/{self.path}:{self.line}"

    @property
    def enclosing(self) -> str:
        return self.name or "(file scope)"


@dataclass(frozen=True)
class Freshness:
    """When, and from which tree, the answer was extracted."""
    updated_at: str
    generator: str
    schema_version: str
    repos: tuple[tuple[str, str, str], ...]  # (repo, commit_sha, indexed_at)

    def describe(self, only: tuple[str, ...] = ()) -> str:
        rows = [r for r in self.repos if not only or r[0] in only] or list(self.repos)
        parts = [f"{repo} @ {(sha or '?')[:12]} ({at or '?'})" for repo, sha, at in rows]
        head = f"calls.db updated_at {self.updated_at or 'unknown'}"
        if self.generator:
            head += f" (generator {self.generator})"
        return head + ("; " + ", ".join(parts) if parts else "")


@dataclass(frozen=True)
class Result:
    targets: tuple[Target, ...]
    callers: tuple[Caller, ...]
    freshness: Freshness
    truncated: bool

    @property
    def repos(self) -> tuple[str, ...]:
        seen = {t.repo for t in self.targets} | {c.repo for c in self.callers}
        return tuple(sorted(seen))


def open_calls_db(path: str | None = None) -> sqlite3.Connection:
    """Read-only-ish connection to calls.db. check_same_thread=False because
    MCP tool calls land on varying worker threads (see mcp/server.py)."""
    return sqlite3.connect(path or default_db_path(), check_same_thread=False)


def parse_lines(lines: str) -> tuple[int, int]:
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


def read_freshness(db: sqlite3.Connection) -> Freshness:
    meta = dict(db.execute(SQL_META).fetchall())
    repos = tuple(db.execute(SQL_REPOS).fetchall())
    return Freshness(
        updated_at=meta.get("updated_at", ""),
        generator=meta.get("generator", ""),
        schema_version=meta.get("schema_version", ""),
        repos=repos,
    )


def find_targets(db: sqlite3.Connection, symbol: str = "", repo: str = "",
                 path: str = "", lines: str = "",
                 limit: int = 100) -> list[Target]:
    """Definitions matched by name, or by path (optionally narrowed to a line
    range — every definition overlapping it, innermost last)."""
    if symbol:
        rows = db.execute(SQL_TARGETS_BY_SYMBOL, {
            "symbol": symbol, "repo": repo, "limit": limit}).fetchall()
    elif path:
        line_from, line_to = parse_lines(lines)
        rows = db.execute(SQL_TARGETS_BY_PATH, {
            "path": path, "repo": repo, "line_from": line_from,
            "line_to": line_to, "limit": limit}).fetchall()
    else:
        raise ValueError("find_callers needs symbol= or path=")
    return [Target(*r) for r in rows]


def find_callers(db: sqlite3.Connection, symbol: str = "", repo: str = "",
                 path: str = "", lines: str = "", k: int = 30) -> Result:
    """Call sites of the definitions matched by `symbol`, or by `path`
    (+ optional `lines` range: definitions there -> references to them ->
    the definitions enclosing those references)."""
    targets = find_targets(db, symbol=symbol, repo=repo, path=path,
                           lines=lines, limit=max(k, 100))
    fresh = read_freshness(db)
    if not targets:
        return Result((), (), fresh, False)

    keys = [f"id{i}" for i in range(len(targets))]
    params: dict[str, object] = {key: t.id for key, t in zip(keys, targets)}
    params["limit"] = k + 1
    rows = db.execute(
        SQL_CALLERS.format(ids=", ".join(":" + key for key in keys)),
        params,
    ).fetchall()

    callers: list[Caller] = []
    seen: set[tuple] = set()
    for row in rows:
        caller = Caller(*row)
        key = (caller.repo, caller.path, caller.line, caller.name)
        if key in seen:  # one line referencing several matched defs counts once
            continue
        seen.add(key)
        callers.append(caller)
    truncated = len(callers) > k
    return Result(tuple(targets), tuple(callers[:k]), fresh, truncated)


def format_result(res: Result, query: str) -> str:
    """Human/agent readable rendering, shared shape with the MCP tool."""
    fresh = res.freshness.describe(res.repos)
    if not res.targets:
        return (f"{query}: calls.db に一致する定義がありません "
                f"(未索引の repo / 名前違い / 索引がまだ古い)\n{fresh}")
    head = [f"# 呼び出し元 {len(res.callers)}{'+' if res.truncated else ''} 件 — {query}",
            "## 対象の定義"]
    head += [f"- {t.location} {t.name} ({t.kind or 'symbol'})" for t in res.targets[:10]]
    if len(res.targets) > 10:
        head.append(f"- … ほか {len(res.targets) - 10} 件")
    if not res.callers:
        head.append("## 呼び出し元\n- 0 件 (この索引の範囲では参照されていません)")
    else:
        head.append("## 呼び出し元")
        head += [f"- {c.location}  in {c.enclosing}"
                 f"{' (' + c.kind + ')' if c.kind else ''}"
                 f"  [{c.role or 'reference'} -> {c.target_name}]"
                 for c in res.callers]
        if res.truncated:
            head.append("- … 打ち切りました (k を上げるとさらに表示されます)")
    head.append(f"\n{fresh}")
    return "\n".join(head)


def _describe_query(args) -> str:
    if args.symbol:
        return f"symbol={args.symbol}" + (f" repo={args.repo}" if args.repo else "")
    where = f"path={args.path}"
    if args.lines:
        where += f":{args.lines}"
    return where + (f" repo={args.repo}" if args.repo else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m indexer.calls",
        description="呼び出し元を calls.db から引く (MCP tool find_callers と同じ検索)")
    ap.add_argument("--db", default=default_db_path(),
                    help=f"calls.db のパス (既定 {default_db_path()})")
    ap.add_argument("--symbol", default="", help="定義の名前で引く")
    ap.add_argument("--path", default="", help="ファイルパスで引く (repo 相対)")
    ap.add_argument("--lines", default="", help="--path を絞る行範囲 (N または N-M)")
    ap.add_argument("--repo", default="", help="org/name に限定")
    ap.add_argument("-k", type=int, default=30, help="呼び出し元の最大件数")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args(argv)

    if not args.symbol and not args.path:
        ap.error("--symbol か --path のどちらかが必要です")
    if not os.path.exists(args.db):
        print(f"呼び出し関係の索引がまだありません: {args.db} "
              f"(scripts/sync-db.sh で同期します)", file=sys.stderr)
        return 2

    db = open_calls_db(args.db)
    try:
        res = find_callers(db, symbol=args.symbol, repo=args.repo,
                           path=args.path, lines=args.lines, k=args.k)
    except sqlite3.DatabaseError as e:
        print(f"calls.db を読めません: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if args.json:
        print(json.dumps({
            "targets": [t.__dict__ for t in res.targets],
            "callers": [c.__dict__ for c in res.callers],
            "freshness": {
                "updated_at": res.freshness.updated_at,
                "generator": res.freshness.generator,
                "schema_version": res.freshness.schema_version,
                "repos": [{"repo": r, "commit_sha": s, "indexed_at": a}
                          for r, s, a in res.freshness.repos],
            },
            "truncated": res.truncated,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_result(res, _describe_query(args)))
    return 0 if res.targets else 1


if __name__ == "__main__":
    raise SystemExit(main())
