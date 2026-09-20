"""SCIP ingest tests.

Everything here runs off the JSON fixtures in `tests/fixtures/`, so neither
the `scip` CLI nor a rust/node toolchain is needed — `.github/workflows/ci.yml`
installs neither.
"""
import json
import os

from indexer import scip

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def _ingest(tmp_path, fixture, repo="org/demo", commit="deadbeef"):
    db = scip.open_db(str(tmp_path / "calls.db"))
    stats = scip.ingest_index(db, repo, _load(fixture), commit)
    return db, stats


# --- SCIP symbol strings -------------------------------------------------

def test_descriptors_splits_sigils():
    assert scip.descriptors(
        "rust-analyzer cargo demo 0.1.0 storage/StorageBackend#download()."
    ) == [("storage", "namespace"), ("StorageBackend", "type"),
          ("download", "method")]


def test_descriptors_handles_backticked_names():
    d = scip.descriptors(
        "scip-typescript npm demo 1.0.0 src/lib/`config.ts`/CacheEntry#value.")
    assert d == [("src", "namespace"), ("lib", "namespace"),
                 ("config.ts", "namespace"), ("CacheEntry", "type"),
                 ("value", "term")]


def test_descriptors_handles_impl_type_parameters():
    d = scip.descriptors(
        "rust-analyzer cargo demo 0.1.0 r2/impl#[R2Backend][StorageBackend]download().")
    assert d == [("r2", "namespace"), ("impl", "type"),
                 ("R2Backend", "type_parameter"),
                 ("StorageBackend", "type_parameter"), ("download", "method")]


def test_symbol_kind_prefers_the_tool_and_falls_back_to_the_sigil():
    sym = "rust-analyzer cargo demo 0.1.0 storage/StorageBackend#download()."
    assert scip.symbol_kind(sym, 70) == "trait_method"
    assert scip.symbol_kind(sym, 0) == "method"
    assert scip.symbol_name(sym) == "download"
    assert scip.symbol_name(sym, "renamed") == "renamed"


# --- ranges --------------------------------------------------------------

def test_span_expands_the_single_line_form():
    assert scip.span([3, 4, 21]) == ((3, 4), (3, 21))
    assert scip.span([3, 4, 9, 1]) == ((3, 4), (9, 1))


def test_resolve_enclosing_picks_the_innermost():
    intervals = [((0, 0), (20, 0), "module"), ((2, 0), (6, 1), "fn")]
    points = [((4, 8), None), ((10, 0), None), ((30, 0), None)]
    assert scip.resolve_enclosing(intervals, points) == ["fn", "module", None]


def test_split_shared_bodies_gives_each_sibling_its_own_slice():
    # what rust-analyzer emits for two `#[async_trait]` impl members: both
    # carry the range of the whole impl block
    block = ((2, 0), (20, 1))
    bodies = [(block[0], block[1], "upload", (3, 13)),
              (block[0], block[1], "download", (10, 13))]
    out = dict(((a, b), sid) for a, b, sid in scip.split_shared_bodies(bodies))
    assert out[((3, 13), (9, scip.END_OF_LINE))] == "upload"
    assert out[((10, 13), (20, 1))] == "download"


def test_split_shared_bodies_leaves_unique_ranges_alone():
    bodies = [((2, 0), (6, 1), "fn", (2, 7))]
    assert scip.split_shared_bodies(bodies) == [((2, 0), (6, 1), "fn")]


# --- ingest --------------------------------------------------------------

def test_ingest_records_definitions_with_their_own_span(tmp_path):
    db, stats = _ingest(tmp_path, "scip_sample.json")
    row = db.execute(
        "SELECT name, kind, path, start_line, end_line FROM symbols "
        "WHERE scip_symbol LIKE '%storage/StorageBackend#download().'"
    ).fetchone()
    assert row == ("download", "trait_method", "src/storage.rs", 4, 4)
    assert stats["documents"] == 3


def test_ingest_attributes_a_call_to_its_enclosing_definition(tmp_path):
    db, _ = _ingest(tmp_path, "scip_sample.json")
    rows = db.execute(
        "SELECT r.path, r.line, e.name, e.kind, r.role FROM refs r "
        "JOIN symbols s ON s.id = r.symbol_id "
        "JOIN symbols e ON e.id = r.enclosing_symbol_id "
        "WHERE s.scip_symbol LIKE '%storage/StorageBackend#download().' "
        "AND r.role = 'reference'"
    ).fetchall()
    assert rows == [("src/app.rs", 5, "fetch_blob", "function", "reference")]


def test_ingest_does_not_blame_the_first_member_of_an_impl_block(tmp_path):
    # net/Client#get() is called from inside `download`, which shares its
    # enclosing_range with `upload`; a naive lookup would credit `upload`.
    db, _ = _ingest(tmp_path, "scip_sample.json")
    row = db.execute(
        "SELECT e.name, e.path, e.start_line FROM refs r "
        "JOIN symbols s ON s.id = r.symbol_id "
        "JOIN symbols e ON e.id = r.enclosing_symbol_id "
        "WHERE s.scip_symbol LIKE '%net/Client#get().'"
    ).fetchone()
    assert row == ("download", "src/r2.rs", 11)
    other = db.execute(
        "SELECT e.name FROM refs r JOIN symbols s ON s.id = r.symbol_id "
        "JOIN symbols e ON e.id = r.enclosing_symbol_id "
        "WHERE s.scip_symbol LIKE '%net/Client#put().'"
    ).fetchone()
    assert other == ("upload",)


def test_ingest_skips_document_scoped_locals(tmp_path):
    db, _ = _ingest(tmp_path, "scip_sample.json")
    assert db.execute(
        "SELECT count(*) FROM symbols WHERE scip_symbol LIKE 'local %'"
    ).fetchone()[0] == 0


def test_ingest_keeps_symbols_it_only_saw_referenced(tmp_path):
    db, _ = _ingest(tmp_path, "scip_sample.json")
    row = db.execute(
        "SELECT name, path FROM symbols WHERE scip_symbol LIKE '%net/Client#get().'"
    ).fetchone()
    assert row == ("get", None)


def test_ingest_derives_implementation_edges_for_rust(tmp_path):
    db, stats = _ingest(tmp_path, "scip_sample.json")
    row = db.execute(
        "SELECT t.scip_symbol, i.path, r.line FROM refs r "
        "JOIN symbols t ON t.id = r.symbol_id "
        "JOIN symbols i ON i.id = r.enclosing_symbol_id "
        "WHERE r.role = 'implementation' AND i.path = 'src/r2.rs' "
        "AND i.name = 'download'"
    ).fetchone()
    assert row[0].endswith("storage/StorageBackend#download().")
    assert (row[1], row[2]) == ("src/r2.rs", 11)
    assert stats["impl_trait_not_local"] == 0


def test_ingest_uses_relationships_when_the_tool_emits_them(tmp_path):
    db, stats = _ingest(tmp_path, "scip_ts_sample.json", repo="org/ts")
    row = db.execute(
        "SELECT t.name, i.name, r.path, r.line FROM refs r "
        "JOIN symbols t ON t.id = r.symbol_id "
        "JOIN symbols i ON i.id = r.enclosing_symbol_id "
        "WHERE r.role = 'implementation'"
    ).fetchone()
    assert row == ("sound", "sound", "src/lib/html.ts", 13)
    assert stats["relationships"] == 1


def test_ingest_falls_back_to_the_module_for_top_level_references(tmp_path):
    db, _ = _ingest(tmp_path, "scip_ts_sample.json", repo="org/ts")
    row = db.execute(
        "SELECT e.name, e.kind FROM refs r "
        "JOIN symbols s ON s.id = r.symbol_id "
        "JOIN symbols e ON e.id = r.enclosing_symbol_id "
        "WHERE s.scip_symbol LIKE '%String#replace().'"
    ).fetchone()
    assert row == ("escapeHtml", "method")


def test_ingest_records_the_repo_and_meta_rows(tmp_path):
    db, _ = _ingest(tmp_path, "scip_sample.json")
    repo, sha, _, tool = db.execute("SELECT * FROM repos").fetchone()
    assert (repo, sha, tool) == ("org/demo", "deadbeef", "rust-analyzer")
    meta = dict(db.execute("SELECT key, value FROM meta"))
    assert meta["generator"] == "indexer.scip"
    assert meta["schema_version"] == scip.SCHEMA_VERSION


def test_two_repos_share_one_db_and_reingest_is_idempotent(tmp_path):
    path = str(tmp_path / "calls.db")
    db = scip.open_db(path)
    scip.ingest_index(db, "org/demo", _load("scip_sample.json"), "aaa")
    scip.ingest_index(db, "org/ts", _load("scip_ts_sample.json"), "bbb")
    before = db.execute("SELECT repo, count(*) FROM refs GROUP BY repo").fetchall()
    scip.ingest_index(db, "org/demo", _load("scip_sample.json"), "ccc")
    assert db.execute(
        "SELECT repo, count(*) FROM refs GROUP BY repo").fetchall() == before
    assert db.execute(
        "SELECT commit_sha FROM repos WHERE repo='org/demo'").fetchone()[0] == "ccc"


def test_cli_writes_a_db(tmp_path, capsys):
    out = str(tmp_path / "calls.db")
    assert scip.main([
        "--repo", "org/demo", "--db", out,
        "--json", os.path.join(FIXTURES, "scip_sample.json"),
        "--commit", "cafe",
    ]) == 0
    assert "org/demo" in capsys.readouterr().out
    db = scip.open_db(out)
    assert db.execute("SELECT count(*) FROM refs").fetchone()[0] > 0
