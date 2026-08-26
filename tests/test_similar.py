import subprocess

from indexer import similar


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_added_ranges_only_flags_chunks_touching_new_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")

    (tmp_path / "f.py").write_text(
        "def old_fn():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout.strip()

    (tmp_path / "f.py").write_text(
        "def old_fn():\n    return 1\n\n\ndef new_fn():\n    return 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add new_fn")

    ranges = similar.added_ranges(base)
    assert "f.py" in ranges and ranges["f.py"]

    items = similar.changed_chunks(ranges)
    symbols = {ch.symbol for _path, ch, _lang in items}
    assert "new_fn" in symbols
    assert "old_fn" not in symbols  # untouched chunk is not re-flagged


def test_added_ranges_empty_when_no_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.md").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout.strip()
    assert similar.added_ranges(base) == {}
