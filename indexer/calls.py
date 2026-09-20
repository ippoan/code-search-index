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
import subprocess
import sys
from dataclasses import dataclass

# --- shared block: the calls.db SQL, and the few helpers both copies need
#     (identical copy in mcp/server.py / indexer/calls.py — tests/test_calls.py
#     compares the two blocks as text, which is what keeps them in step) ---
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


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def head_rev(repo_dir: str) -> str:
    """The revision checked out at `repo_dir`, or "" when it is not a git
    checkout. Shelling out rather than reading .git ourselves: it is shorter,
    and it is right inside a worktree (where .git is a file) and with packed
    refs — the cases hand-rolled parsing gets wrong. ~5 ms, once per answer.
    """
    try:
        done = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def code_note(started_rev: str, repo_dir: str) -> str:
    """Which revision of *this code* produced the answer, and whether the
    checkout has moved past it — a process reads its code once, at startup.

    The index already says how fresh its data is; the code said nothing, which
    is how this server ran two merges behind without anyone noticing.
    """
    if not started_rev:
        return ""
    now = head_rev(repo_dir)
    if now and now != started_rev:
        return (f"; code @ {started_rev} — 作業ツリーは {now} に進んでいます "
                "(セッションを開き直すと新しいコードで動きます)")
    return f"; code @ {started_rev}"


STARTED_REV = head_rev(REPO_DIR)
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
    via_impl: bool = False   # reached by following an implementation row

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
        return (head + ("; " + ", ".join(parts) if parts else "")
                + code_note(STARTED_REV, REPO_DIR))


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


def follow_implementations(db: sqlite3.Connection, targets: list[Target],
                           limit: int = 100) -> list[Target]:
    """Add the members each target implements.

    A call written against a trait/interface resolves to the *implemented*
    member, so a concrete impl has no references of its own — asking about it
    answered "0 callers" while the trait method had 62 (rust-alc-api,
    R2Backend::download vs StorageBackend::download). One hop fixes that.
    """
    sql, params = _by_ids(SQL_IMPL_TARGETS, [t.id for t in targets], limit)
    known = {t.id for t in targets}
    return targets + [Target(*row, via_impl=True)
                      for row in db.execute(sql, params).fetchall()
                      if row[0] not in known]


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
    targets = follow_implementations(db, targets)

    sql, params = _by_ids(SQL_CALLERS, [t.id for t in targets], k + 1)
    rows = db.execute(sql, params).fetchall()

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
    head += [f"- {t.location} {t.name} ({t.kind or 'symbol'})"
             + ("  ← 実装元 (trait/interface 越しの呼び出しはここに解決される)"
                if t.via_impl else "")
             for t in res.targets[:10]]
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
