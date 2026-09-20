"""Ingest SCIP indexes into `calls.db` — the call-graph side of the index.

The semantic index (`code-index.db`) is a catalogue of definitions; it cannot
answer "who calls this". SCIP can: every occurrence of a symbol is recorded
with its range, and definition occurrences additionally carry an
`enclosing_range` covering the whole body. Intersecting the two gives the
caller of each reference, which is what `refs.enclosing_symbol_id` holds.

Input is the JSON form of a SCIP index (`scip print --json <index.scip>`), so
no protobuf parser is needed here.

Usage:
  python -m indexer.scip --repo ippoan/auth-worker --commit <sha> \
      --json auth-worker.json --db calls.db [--tool scip-typescript]

`--repo` is re-ingestable: existing rows for that repo are dropped first, so
the two matrix legs of `.github/workflows/scip.yml` can write into one DB.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3

SCHEMA_VERSION = "1"

# scip.proto SymbolInformation.Kind. rust-analyzer fills this in;
# scip-typescript 0.4.0 does not, so `symbol_kind()` falls back to the
# descriptor suffix of the SCIP symbol string.
KINDS = {
    1: "array", 2: "assertion", 3: "associated_type", 4: "attribute",
    5: "axiom", 6: "boolean", 7: "class", 8: "constant", 9: "constructor",
    10: "data_family", 11: "enum", 12: "enum_member", 13: "event", 14: "fact",
    15: "field", 16: "file", 17: "function", 18: "getter", 19: "grammar",
    20: "instance", 21: "interface", 22: "key", 23: "lang", 24: "lemma",
    25: "macro", 26: "method", 27: "method_receiver", 28: "message",
    29: "module", 30: "namespace", 31: "null", 32: "number", 33: "object",
    34: "operator", 35: "package", 36: "package_object", 37: "parameter",
    38: "parameter_label", 39: "pattern", 40: "predicate", 41: "property",
    42: "protocol", 43: "quasiquoter", 44: "self_parameter", 45: "setter",
    46: "signature", 47: "subscript", 48: "string", 49: "struct",
    50: "tactic", 51: "theorem", 52: "this_parameter", 53: "trait",
    54: "type", 55: "type_alias", 56: "type_class", 57: "type_family",
    58: "type_parameter", 59: "union", 60: "value", 61: "variable",
    62: "contract", 63: "error", 64: "library", 65: "modifier",
    66: "abstract_method", 67: "method_specification", 68: "protocol_method",
    69: "pure_virtual_method", 70: "trait_method", 71: "type_class_method",
    72: "accessor", 73: "delegate", 74: "method_alias", 75: "singleton_class",
    76: "singleton_method", 77: "static_data_member", 78: "static_event",
    79: "static_field", 80: "static_method", 81: "static_property",
    82: "static_variable", 84: "extension", 85: "mixin", 86: "concept",
}

# SymbolRole bit flags (scip.proto). Only Definition is load-bearing here.
ROLE_DEFINITION = 0x1

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS repos(
    repo TEXT PRIMARY KEY, commit_sha TEXT, indexed_at TEXT, tool TEXT);
CREATE TABLE IF NOT EXISTS symbols(
    id INTEGER PRIMARY KEY,
    repo TEXT, scip_symbol TEXT,
    name TEXT, kind TEXT,
    path TEXT, start_line INT, end_line INT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
    ON symbols(repo, scip_symbol);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON symbols(repo, name);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_path ON symbols(repo, path);
CREATE TABLE IF NOT EXISTS refs(
    symbol_id INTEGER,
    repo TEXT, path TEXT, line INT,
    enclosing_symbol_id INTEGER,
    role TEXT);
CREATE INDEX IF NOT EXISTS idx_refs_symbol ON refs(symbol_id);
CREATE INDEX IF NOT EXISTS idx_refs_enclosing ON refs(enclosing_symbol_id);
"""


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def set_meta(db: sqlite3.Connection, key: str, value: str):
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# --- SCIP symbol strings -------------------------------------------------
#
# <symbol> ::= <scheme> ' ' <manager> ' ' <package> ' ' <version> {<descriptor>}
# Descriptors end in a sigil that says what they are: '/' namespace, '#' type,
# '.' term, ':' meta, '!' macro, '().' method, '[x]' type parameter,
# '(x)' parameter. Names may be backtick-quoted (doubled backtick escapes one),
# which is how paths like `config.ts` survive the sigils.

_SIGILS = {"/": "namespace", "#": "type", ".": "term", ":": "meta",
           "!": "macro"}


def descriptors(symbol: str) -> list[tuple[str, str]]:
    """Split the descriptor tail of a SCIP symbol into (name, kind) pairs."""
    parts = symbol.split(" ", 4)
    if len(parts) < 5:
        return []
    s = parts[4]
    out: list[tuple[str, str]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "[":                       # type parameter
            j = s.find("]", i)
            if j < 0:
                break
            out.append((s[i + 1:j], "type_parameter"))
            i = j + 1
            continue
        if c == "(":                       # parameter
            j = s.find(")", i)
            if j < 0:
                break
            out.append((s[i + 1:j], "parameter"))
            i = j + 1
            continue
        if c == "`":                       # backtick-quoted name
            buf, j = [], i + 1
            while j < n:
                if s[j] == "`":
                    if j + 1 < n and s[j + 1] == "`":
                        buf.append("`")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            name, i = "".join(buf), j + 1
        else:
            j = i
            while j < n and s[j] not in "/#.:!([":
                j += 1
            name, i = s[i:j], j
        if i >= n:
            break
        if s[i] == "(":                    # method: name '(' disambiguator ').'
            j = s.find(")", i)
            if j < 0:
                break
            i = j + 1
            if i < n and s[i] == ".":
                i += 1
            out.append((name, "method"))
            continue
        kind = _SIGILS.get(s[i])
        i += 1
        if kind is None:
            continue
        out.append((name, kind))
    return out


def symbol_name(symbol: str, display_name: str | None = None) -> str:
    """Display name: the tool's own if it gave one, else the last descriptor."""
    if display_name:
        return display_name
    d = descriptors(symbol)
    return d[-1][0] if d else symbol


def symbol_kind(symbol: str, kind: int | None = None) -> str:
    """`kind` is SymbolInformation.kind; 0/None means the tool did not say.

    The descriptor fallback follows the SCIP grammar, which has no separate
    sigil for free functions — `readKey().` and `Cache#get().` both come back
    as "method". Consumers that want "callable" should accept both.
    """
    if kind:
        named = KINDS.get(kind)
        if named:
            return named
    d = descriptors(symbol)
    return d[-1][1] if d else "unknown"


# --- ranges --------------------------------------------------------------
#
# SCIP ranges are [startLine, startChar, endLine, endChar], shortened to
# [line, startChar, endChar] when start and end are on the same line.

def span(rng: list[int]) -> tuple[tuple[int, int], tuple[int, int]]:
    if len(rng) == 3:
        return (rng[0], rng[1]), (rng[0], rng[2])
    return (rng[0], rng[1]), (rng[2], rng[3])


END_OF_LINE = 1 << 30


def split_shared_bodies(bodies):
    """Give each definition its own slice when siblings report one range.

    rust-analyzer hands every member of an `#[async_trait]` impl the range of
    the whole impl block, so a naive innermost-interval lookup blames the
    first method in the block for calls made by all the others (16.5% of
    definitions in rust-alc-api). Where a range is claimed by more than one
    definition, each member instead owns from its own name down to the next
    member's name, which is how the source actually reads.

    `bodies` is [(start, end, symbol_id, name_start)]; returns
    [(start, end, symbol_id)].
    """
    groups: dict[tuple, list] = {}
    for body in bodies:
        groups.setdefault((body[0], body[1]), []).append(body)
    out = []
    for (start, end), members in groups.items():
        if len(members) == 1:
            out.append((start, end, members[0][2]))
            continue
        members.sort(key=lambda b: b[3])
        for i, (_, _, sid, name_start) in enumerate(members):
            if i + 1 < len(members):
                # up to the line before the next member's name
                stop = (max(members[i + 1][3][0] - 1, name_start[0]), END_OF_LINE)
            else:
                stop = end
            out.append((name_start, stop, sid))
    return out


def resolve_enclosing(intervals, points):
    """Innermost containing interval for each point.

    `intervals` is [(start, end, payload)], `points` is [(pos, index)]; both
    positions are (line, char). Returns a list of payloads (or None) aligned
    with `points`. Definition bodies nest, so a stack sweep is enough: the
    innermost still-open interval is the top of the stack.
    """
    intervals = sorted(intervals, key=lambda iv: (iv[0], [-v for v in iv[1]]))
    order = sorted(range(len(points)), key=lambda k: points[k][0])
    out: list = [None] * len(points)
    stack: list = []
    i = 0
    for k in order:
        pos = points[k][0]
        while i < len(intervals) and intervals[i][0] <= pos:
            start, end, payload = intervals[i]
            while stack and stack[-1][1] < start:
                stack.pop()
            stack.append((start, end, payload))
            i += 1
        while stack and stack[-1][1] < pos:
            stack.pop()
        if stack:
            out[k] = stack[-1][2]
    return out


# --- ingest --------------------------------------------------------------

class SymbolTable:
    """Interns SCIP symbol strings into `symbols` rows for one repo."""

    def __init__(self, db: sqlite3.Connection, repo: str):
        self.db = db
        self.repo = repo
        self.ids: dict[str, int] = {}

    def id_for(self, symbol: str) -> int:
        sid = self.ids.get(symbol)
        if sid is not None:
            return sid
        cur = self.db.execute(
            "INSERT INTO symbols(repo, scip_symbol, name, kind) VALUES(?,?,?,?)",
            (self.repo, symbol, symbol_name(symbol), symbol_kind(symbol)),
        )
        sid = cur.lastrowid
        self.ids[symbol] = sid
        return sid

    def define(self, symbol: str, path: str, start_line: int, end_line: int,
               name: str, kind: str):
        sid = self.id_for(symbol)
        self.db.execute(
            "UPDATE symbols SET name=?, kind=?, path=?, start_line=?, end_line=? "
            "WHERE id=?",
            (name, kind, path, start_line, end_line, sid),
        )
        return sid


def derive_impl_edges(db: sqlite3.Connection, repo: str) -> tuple[int, int]:
    """Add the `implementation` edges rust-analyzer does not emit.

    scip-typescript states "Dog#sound() implements Animal#sound()" as a
    SymbolInformation relationship; rust-analyzer (1.97) emits none. It does,
    however, name impl members after the trait they satisfy —
    `r2/impl#[R2Backend][StorageBackend]download().` — so the edge can be
    recovered by matching that trait descriptor against a locally defined
    method of the same name on a type of that name.

    Returns (edges added, impl members whose trait is not defined in this
    repo — `From`, `Debug` and friends, which is expected and not an error).
    """
    by_owner: dict[tuple[str, str], list[int]] = {}
    impls: list[tuple[int, str, str, str, int]] = []
    rows = db.execute(
        "SELECT id, scip_symbol, path, start_line FROM symbols "
        "WHERE repo=? AND path IS NOT NULL", (repo,)).fetchall()
    for sid, symbol, path, line in rows:
        d = descriptors(symbol)
        if not d or d[-1][1] != "method":
            continue
        name = d[-1][0]
        head = d[:-1]
        if any(n == "impl" and k == "type" for n, k in head):
            params = [n for n, k in head if k == "type_parameter"]
            if len(params) >= 2:                    # [Type][Trait]method()
                impls.append((sid, symbol, params[-1], name, line))
                continue
        owners = [n for n, k in head if k == "type"]
        if owners:
            by_owner.setdefault((owners[-1], name), []).append(sid)

    added = unresolved = 0
    for sid, symbol, trait, name, line in impls:
        candidates = by_owner.get((trait, name))
        if not candidates or len(candidates) != 1:
            unresolved += 1
            continue
        path = db.execute("SELECT path FROM symbols WHERE id=?",
                          (sid,)).fetchone()[0]
        db.execute(
            "INSERT INTO refs(symbol_id, repo, path, line, "
            "enclosing_symbol_id, role) VALUES(?,?,?,?,?,?)",
            (candidates[0], repo, path, line, sid, "implementation"),
        )
        added += 1
    return added, unresolved


def ingest_index(db: sqlite3.Connection, repo: str, index: dict,
                 commit_sha: str = "", tool: str = "") -> dict:
    """Load one parsed SCIP index (`scip print --json`) for `repo`."""
    tool = tool or (index.get("metadata", {}).get("tool_info", {}) or {}).get(
        "name", "")
    db.execute("DELETE FROM refs WHERE repo=?", (repo,))
    db.execute("DELETE FROM symbols WHERE repo=?", (repo,))
    db.execute("DELETE FROM repos WHERE repo=?", (repo,))

    table = SymbolTable(db, repo)
    stats = {"documents": 0, "symbols": 0, "refs": 0, "refs_unresolved": 0,
             "impl_trait_not_local": 0,
             "relationships": 0}

    for doc in index.get("documents", []):
        path = doc.get("relative_path", "")
        stats["documents"] += 1

        # SymbolInformation carries kind / display_name when the tool emits
        # them (rust-analyzer does, scip-typescript does not).
        info = {}
        for si in doc.get("symbols", []) or []:
            info[si["symbol"]] = si

        bodies: list = []     # (start, end, symbol_id, name_start) for defs
        points: list = []     # ((line, char), symbol_id, role)
        for occ in doc.get("occurrences", []):
            symbol = occ["symbol"]
            # `local N` is document-scoped, so the same string means different
            # things in different files and would collide on
            # UNIQUE(repo, scip_symbol). Locals are never call-graph edges.
            if symbol.startswith("local "):
                continue
            start, end = span(occ["range"])
            if occ.get("symbol_roles", 0) & ROLE_DEFINITION:
                si = info.get(symbol, {})
                body = occ.get("enclosing_range")
                bstart, bend = span(body) if body else (start, end)
                sid = table.define(
                    symbol, path, bstart[0] + 1, bend[0] + 1,
                    symbol_name(symbol, si.get("display_name")),
                    symbol_kind(symbol, si.get("kind")),
                )
                if body:
                    bodies.append((bstart, bend, sid, start))
            else:
                points.append((start, table.id_for(symbol), "reference"))

        # Siblings that shared one range now have narrower spans; the
        # symbols row should report the same slice the lookup uses.
        spans = split_shared_bodies(bodies)
        for sstart, send, sid in spans:
            db.execute("UPDATE symbols SET start_line=?, end_line=? WHERE id=?",
                       (sstart[0] + 1, send[0] + 1, sid))
        enclosing = resolve_enclosing(spans, points)
        for (pos, sid, role), encl in zip(points, enclosing):
            if encl is None:
                stats["refs_unresolved"] += 1
            db.execute(
                "INSERT INTO refs(symbol_id, repo, path, line, "
                "enclosing_symbol_id, role) VALUES(?,?,?,?,?,?)",
                (sid, repo, path, pos[0] + 1, encl, role),
            )
            stats["refs"] += 1

        # Relationships are the edges an occurrence cannot express: which
        # trait/interface member a definition implements. scip-typescript
        # emits them; rust-analyzer (1.97) does not.
        for si in doc.get("symbols", []) or []:
            for rel in si.get("relationships", []) or []:
                for flag, role in (("is_implementation", "implementation"),
                                   ("is_type_definition", "type_definition")):
                    if not rel.get(flag):
                        continue
                    impl_id = table.id_for(si["symbol"])
                    row = db.execute(
                        "SELECT path, start_line FROM symbols WHERE id=?",
                        (impl_id,)).fetchone()
                    db.execute(
                        "INSERT INTO refs(symbol_id, repo, path, line, "
                        "enclosing_symbol_id, role) VALUES(?,?,?,?,?,?)",
                        (table.id_for(rel["symbol"]), repo,
                         row[0] if row else path, row[1] if row else None,
                         impl_id, role),
                    )
                    stats["relationships"] += 1

    derived, unlinked = derive_impl_edges(db, repo)
    stats["relationships"] += derived
    stats["impl_trait_not_local"] = unlinked

    stats["symbols"] = db.execute(
        "SELECT count(*) FROM symbols WHERE repo=?", (repo,)).fetchone()[0]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    db.execute(
        "INSERT INTO repos(repo, commit_sha, indexed_at, tool) VALUES(?,?,?,?)",
        (repo, commit_sha, now, tool),
    )
    set_meta(db, "generator", "indexer.scip")
    set_meta(db, "schema_version", SCHEMA_VERSION)
    set_meta(db, "updated_at", now)
    db.commit()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="org/name")
    ap.add_argument("--json", required=True, help="scip print --json output")
    ap.add_argument("--db", default="calls.db")
    ap.add_argument("--commit", default="")
    ap.add_argument("--tool", default="")
    args = ap.parse_args(argv)

    with open(args.json) as fh:
        index = json.load(fh)
    db = open_db(args.db)
    stats = ingest_index(db, args.repo, index, args.commit, args.tool)
    db.close()
    unresolved = stats["refs_unresolved"]
    pct = 100.0 * unresolved / stats["refs"] if stats["refs"] else 0.0
    print(
        f"{args.repo}: {stats['documents']} documents, {stats['symbols']} symbols, "
        f"{stats['refs']} refs (+{stats['relationships']} relationship edges), "
        f"{unresolved} without an enclosing definition ({pct:.2f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
