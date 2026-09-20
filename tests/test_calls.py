"""Call-graph queries (indexer/calls.py) against a hand-built calls.db.

The fixture follows the published schema exactly, so these tests do not wait
for the SCIP extraction job to produce a real DB.
"""
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from indexer import calls

ROOT = pathlib.Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE repos(repo TEXT PRIMARY KEY, commit_sha TEXT, indexed_at TEXT, tool TEXT);
CREATE TABLE symbols(
    id INTEGER PRIMARY KEY,
    repo TEXT, scip_symbol TEXT,
    name TEXT, kind TEXT,
    path TEXT, start_line INT, end_line INT);
CREATE UNIQUE INDEX idx_symbols_unique ON symbols(repo, scip_symbol);
CREATE INDEX idx_symbols_repo_name ON symbols(repo, name);
CREATE INDEX idx_symbols_repo_path ON symbols(repo, path);
CREATE TABLE refs(
    symbol_id INTEGER,
    repo TEXT, path TEXT, line INT,
    enclosing_symbol_id INTEGER,
    role TEXT);
CREATE INDEX idx_refs_symbol ON refs(symbol_id);
CREATE INDEX idx_refs_enclosing ON refs(enclosing_symbol_id);
"""

# id, repo, scip_symbol, name, kind, path, start, end
SYMBOLS = [
    (1, "ippoan/alpha", "scip:alpha Repo#save().", "save", "method",
     "src/repo.rs", 10, 20),
    (2, "ippoan/alpha", "scip:alpha handler().", "handler", "function",
     "src/router.rs", 5, 30),
    (3, "ippoan/alpha", "scip:alpha SqlRepo#save().", "save", "method",
     "src/sql.rs", 40, 60),
    (4, "ippoan/alpha", "scip:alpha lonely().", "lonely", "function",
     "src/misc.rs", 1, 3),
    (5, "ippoan/beta", "scip:beta run().", "run", "function",
     "src/lib.rs", 100, 120),
    (6, "ippoan/alpha", "scip:alpha Repo#", "Repo", "trait",
     "src/repo.rs", 8, 25),
    # trait / impl / module — the shape measured in rust-alc-api
    (7, "ippoan/alpha", "scip:alpha `r2`/", "r2", "module",
     "src/r2.rs", 1, 150),
    (8, "ippoan/alpha", "scip:alpha StorageBackend#download().", "download",
     "trait_method", "src/storage.rs", 39, 41),
    (9, "ippoan/alpha", "scip:alpha R2Backend#download().", "download",
     "method", "src/r2.rs", 88, 95),
    (10, "ippoan/alpha", "scip:alpha user().", "user", "function",
     "src/user.rs", 10, 20),
]

# symbol_id, repo, path, line, enclosing_symbol_id, role
REFS = [
    # handler() calls Repo::save — the call grep cannot see (through the trait)
    (1, "ippoan/alpha", "src/router.rs", 12, 2, "reference"),
    # SqlRepo::save implements it
    (1, "ippoan/alpha", "src/sql.rs", 41, 3, "implementation"),
    # another repo calls it too
    (1, "ippoan/beta", "src/lib.rs", 105, 5, "reference"),
    # the same line also names the trait itself (deduped into one caller row)
    (6, "ippoan/alpha", "src/router.rs", 12, 2, "reference"),
    # a reference outside any definition -> file scope
    (2, "ippoan/alpha", "src/main.rs", 3, None, "reference"),
    # R2Backend::download implements StorageBackend::download
    (8, "ippoan/alpha", "src/r2.rs", 88, 9, "implementation"),
    # the call site resolves to the trait method, never to the impl
    (8, "ippoan/alpha", "src/user.rs", 15, 10, "reference"),
    # `use` of the module spanning the file — noise a line range must not pull in
    (7, "ippoan/alpha", "src/lib.rs", 7, None, "reference"),
]


@pytest.fixture()
def db(tmp_path):
    con = sqlite3.connect(str(tmp_path / "calls.db"))
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)", SYMBOLS)
    con.executemany("INSERT INTO refs VALUES(?,?,?,?,?,?)", REFS)
    con.executemany("INSERT INTO repos VALUES(?,?,?,?)", [
        ("ippoan/alpha", "a" * 40, "2026-09-20T10:00:00+00:00", "scip-rust"),
        ("ippoan/beta", "b" * 40, "2026-09-20T10:05:00+00:00", "scip-rust"),
    ])
    con.executemany("INSERT INTO meta VALUES(?,?)", [
        ("generator", "indexer.scip"),
        ("updated_at", "2026-09-20T10:06:00+00:00"),
        ("schema_version", "1"),
    ])
    con.commit()
    yield con
    con.close()


def test_symbol_route_lists_callers_across_repos(db):
    res = calls.find_callers(db, symbol="save")

    assert [t.location for t in res.targets] == [
        "ippoan/alpha/src/repo.rs:10", "ippoan/alpha/src/sql.rs:40"]
    assert [c.location for c in res.callers] == [
        "ippoan/alpha/src/router.rs:12",
        "ippoan/alpha/src/sql.rs:41",
        "ippoan/beta/src/lib.rs:105",
    ]
    by_loc = {c.location: c for c in res.callers}
    # the trait-dispatched call site names its enclosing definition
    assert by_loc["ippoan/alpha/src/router.rs:12"].enclosing == "handler"
    assert by_loc["ippoan/alpha/src/sql.rs:41"].role == "implementation"
    assert not res.truncated


def test_repo_filter_narrows_the_definition_not_the_callers(db):
    res = calls.find_callers(db, symbol="save", repo="ippoan/alpha")
    assert {t.repo for t in res.targets} == {"ippoan/alpha"}
    # a caller in another repo still counts — that is the point of the index
    assert "ippoan/beta/src/lib.rs:105" in {c.location for c in res.callers}

    assert calls.find_callers(db, symbol="save", repo="ippoan/beta").targets == ()


def test_path_and_line_route_finds_the_definition_at_those_lines(db):
    res = calls.find_callers(db, path="src/repo.rs", lines="12-14")

    # the enclosing trait (8-25) is dropped: the innermost match is the answer
    assert [t.name for t in res.targets] == ["save"]
    assert [c.location for c in res.callers] == [
        "ippoan/alpha/src/router.rs:12",
        "ippoan/alpha/src/sql.rs:41",
        "ippoan/beta/src/lib.rs:105",
    ]


def test_path_route_without_lines_takes_the_whole_file(db):
    res = calls.find_callers(db, path="src/repo.rs")
    assert {t.name for t in res.targets} == {"Repo", "save"}
    # router.rs:12 references both matched definitions -> one caller row
    assert [c.location for c in res.callers] == [
        "ippoan/alpha/src/router.rs:12",
        "ippoan/alpha/src/sql.rs:41",
        "ippoan/beta/src/lib.rs:105",
    ]


def test_single_line_and_out_of_range_lines(db):
    assert [t.name for t in calls.find_callers(
        db, path="src/repo.rs", lines="9").targets] == ["Repo"]
    assert calls.find_callers(db, path="src/repo.rs", lines="100-200").targets == ()


def test_reference_outside_any_definition_is_file_scope(db):
    res = calls.find_callers(db, symbol="handler")
    assert [(c.location, c.enclosing) for c in res.callers] == [
        ("ippoan/alpha/src/main.rs:3", "(file scope)")]


def test_no_match_reports_freshness_not_an_error(db):
    res = calls.find_callers(db, symbol="does_not_exist")
    assert res.targets == () and res.callers == ()
    text = calls.format_result(res, "symbol=does_not_exist")
    assert "一致する定義がありません" in text
    assert "2026-09-20T10:06:00+00:00" in text  # meta.updated_at still answered


def test_definition_with_no_callers(db):
    res = calls.find_callers(db, symbol="lonely")
    assert [t.name for t in res.targets] == ["lonely"]
    assert res.callers == ()
    assert "0 件" in calls.format_result(res, "symbol=lonely")


def test_freshness_carries_commit_sha_and_updated_at(db):
    res = calls.find_callers(db, symbol="save")
    assert res.freshness.updated_at == "2026-09-20T10:06:00+00:00"
    assert res.freshness.generator == "indexer.scip"
    assert dict((r, s) for r, s, _ in res.freshness.repos) == {
        "ippoan/alpha": "a" * 40, "ippoan/beta": "b" * 40}

    text = calls.format_result(res, "symbol=save")
    assert "ippoan/alpha @ aaaaaaaaaaaa" in text   # the tree it was extracted from
    assert "ippoan/beta @ bbbbbbbbbbbb" in text
    assert "calls.db updated_at 2026-09-20T10:06:00+00:00" in text


def test_k_truncates_and_says_so(db):
    res = calls.find_callers(db, symbol="save", k=1)
    assert len(res.callers) == 1 and res.truncated
    assert "打ち切りました" in calls.format_result(res, "symbol=save")


def test_needs_symbol_or_path(db):
    with pytest.raises(ValueError):
        calls.find_callers(db)


def test_parse_lines():
    assert calls.parse_lines("") == (0, 0)
    assert calls.parse_lines(" 12 ") == (12, 12)
    assert calls.parse_lines("40-80") == (40, 80)
    assert calls.parse_lines("80-40") == (40, 80)
    with pytest.raises(ValueError):
        calls.parse_lines("abc")


def _cli(*args, expect=0):
    proc = subprocess.run(
        [sys.executable, "-m", "indexer.calls", *args],
        cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == expect, proc.stderr
    return proc.stdout + proc.stderr


def test_cli_matches_the_library(tmp_path, db):
    path = str(tmp_path / "calls.db")
    out = _cli("--db", path, "--symbol", "save")
    assert "ippoan/alpha/src/router.rs:12" in out
    assert "handler" in out
    assert "calls.db updated_at" in out

    out = _cli("--db", path, "--path", "src/repo.rs", "--lines", "12", "--json")
    assert '"ippoan/alpha"' in out and '"commit_sha"' in out


def test_cli_without_a_db_says_so(tmp_path):
    out = _cli("--db", str(tmp_path / "missing.db"), "--symbol", "save", expect=2)
    assert "呼び出し関係の索引がまだありません" in out


def test_cli_exit_1_when_nothing_matches(tmp_path, db):
    _cli("--db", str(tmp_path / "calls.db"), "--symbol", "nope", expect=1)


def test_asking_about_a_concrete_impl_follows_the_trait(db):
    """The call grep cannot see, and neither could one hop: references land on
    the implemented member, so the impl itself has none of its own."""
    res = calls.find_callers(db, path="src/r2.rs", lines="88-95")

    assert [(t.location, t.via_impl) for t in res.targets] == [
        ("ippoan/alpha/src/r2.rs:88", False),        # what was asked about
        ("ippoan/alpha/src/storage.rs:39", True),    # what calls resolve to
    ]
    assert [(c.location, c.role) for c in res.callers] == [
        ("ippoan/alpha/src/r2.rs:88", "implementation"),
        ("ippoan/alpha/src/user.rs:15", "reference"),
    ]
    assert "実装元" in calls.format_result(res, "path=src/r2.rs:88-95")


def test_a_line_range_drops_the_module_spanning_the_file(db):
    """Without this, asking about one method answers with every `use` of the
    module it lives in — noise that crowds out the real callers."""
    res = calls.find_callers(db, path="src/r2.rs", lines="88-95")
    assert "r2" not in {t.name for t in res.targets}
    assert "ippoan/alpha/src/lib.rs:7" not in {c.location for c in res.callers}


def test_without_lines_the_whole_file_still_includes_the_module(db):
    res = calls.find_callers(db, path="src/r2.rs")
    assert "r2" in {t.name for t in res.targets}
    assert "ippoan/alpha/src/lib.rs:7" in {c.location for c in res.callers}


def test_the_hop_does_not_duplicate_an_already_matched_target(db):
    res = calls.find_callers(db, symbol="download")  # matches trait AND impl
    assert len(res.targets) == len({t.id for t in res.targets}) == 2


def test_plain_references_do_not_pull_in_extra_targets(db):
    """Only role='implementation' rows hop; an ordinary reference must not."""
    res = calls.find_callers(db, symbol="handler")
    assert [t.name for t in res.targets] == ["handler"]


# --- drift guard -----------------------------------------------------------
# mcp/server.py cannot import indexer (see the module docstrings), so it keeps
# its own copy of the SQL. Read both files as text and require the blocks to be
# identical — a change to one side without the other fails here.
SQL_START = "# --- calls SQL"
SQL_END = "# --- end calls SQL ---"


def _sql_block(relpath: str) -> str:
    text = (ROOT / relpath).read_text(encoding="utf-8")
    start = text.index(SQL_START)
    end = text.index(SQL_END, start) + len(SQL_END)
    return text[start:end]


def test_calls_sql_block_is_identical_in_both_copies():
    block = _sql_block("indexer/calls.py")
    assert "SQL_CALLERS" in block and "enclosing_symbol_id" in block
    assert block == _sql_block("mcp/server.py"), (
        "indexer/calls.py と mcp/server.py の calls SQL がずれています — "
        "片方だけ直さないこと (server.py は indexer を import できません)")


def test_sql_constants_in_the_module_come_from_that_block():
    block = _sql_block("indexer/calls.py")
    for const in (calls.SQL_TARGETS_BY_SYMBOL, calls.SQL_TARGETS_BY_PATH,
                  calls.SQL_CALLERS, calls.SQL_META, calls.SQL_REPOS):
        assert const.strip() in block
