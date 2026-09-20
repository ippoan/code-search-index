import subprocess

from indexer.search import Hit
from ranker import dataset


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_clean_query_drops_noise_and_pr_suffix():
    assert dataset.clean_query("feat(auth): resolve tenant from KV (#12)") == \
        "feat(auth): resolve tenant from KV"
    assert dataset.clean_query("Merge pull request #3 from x/y") is None
    assert dataset.clean_query("Bump serde from 1.0.1 to 1.0.2") is None
    assert dataset.clean_query("wip") is None
    assert dataset.clean_query("fix typo") is None  # too short to be a query


def test_indexable_files_keeps_indexed_paths_only():
    got = dataset.indexable_files([
        ("M", "src/tenant.ts"),
        ("M", "README.md"),               # indexed language -> still a label
        ("A", "src/tenant.test.ts"),      # EXCLUDE_RE
        ("D", "src/gone.ts"),             # deleted -> nothing to inject
        ("M", "src/logo.png"),            # not chunked
        ("M", "node_modules/x/i.js"),     # gitsync.wanted
    ])
    assert got == ["src/tenant.ts", "README.md"]


def test_collect_queries_reads_subject_and_changed_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial commit")
    (repo / "a.py").write_text("def f():\n    return 2\n")
    (repo / "b.py").write_text("def g():\n    return 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: return two instead of one")

    rows = dataset.collect_queries(str(tmp_path.parent), "org/r", tmp_path.name,
                                   since="2000-01-01")
    assert len(rows) == 1  # root commit has no parent diff, and is "initial commit"
    assert rows[0]["query"] == "feat: return two instead of one"
    assert sorted(rows[0]["files"]) == ["a.py", "b.py"]
    assert rows[0]["repo"] == "org/r" and rows[0]["ts"] > 0


def test_collect_queries_skips_wide_sweeps(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "seed.py").write_text("x = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial commit")
    for i in range(dataset.MAX_FILES + 1):
        (repo / f"f{i}.py").write_text(f"y = {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "chore: reformat the whole tree")
    assert dataset.collect_queries(str(tmp_path.parent), "org/r", tmp_path.name,
                                   since="2000-01-01") == []


def test_label_marks_chunks_from_touched_files():
    group = {"repo": "org/r", "files": ["src/a.rs"]}
    hits = [
        Hit("org/r", "src/a.rs", 1, 9, "f", "rust", 300, 0.4),
        Hit("org/r", "src/b.rs", 1, 9, "g", "rust", 300, 0.5),
        Hit("org/other", "src/a.rs", 1, 9, "h", "rust", 300, 0.6),  # other repo
    ]
    assert [c["label"] for c in dataset.label(group, hits)] == [1, 0, 0]
