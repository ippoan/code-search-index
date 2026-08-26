from indexer import chunker

RUST = """\
use std::fmt;

pub struct Event { pub at: u64 }

impl Event {
    pub fn is_rest(&self) -> bool { self.at == 0 }
}

pub fn fold_events(events: &[Event]) -> Vec<u64> {
    events.iter().map(|e| e.at).collect()
}
"""


def test_rust_functions_are_chunked_with_symbols():
    chunks = chunker.chunk_file(RUST, ".rs")
    symbols = {c.symbol for c in chunks}
    assert "Event" in symbols
    assert "fold_events" in symbols


def test_rust_chunk_content_and_lines():
    chunks = chunker.chunk_file(RUST, ".rs")
    fold = next(c for c in chunks if c.symbol == "fold_events")
    assert "map(|e| e.at)" in fold.text
    assert fold.start_line == 9 and fold.end_line == 11


def test_oversized_container_descends_into_methods():
    body = "\n".join(f"    pub fn m{i}(&self) -> u32 {{ {i} }}" for i in range(300))
    src = f"impl Big {{\n{body}\n}}\n"
    chunks = chunker.chunk_file(src, ".rs")
    # impl exceeds MAX_CHUNK_LINES, so individual methods must be captured
    assert any(c.symbol == "m0" for c in chunks)
    assert any(c.symbol == "m299" for c in chunks)
    assert all(c.end_line - c.start_line < chunker.MAX_CHUNK_LINES for c in chunks)


def test_typescript_and_python_capture():
    ts = "export function hello(name: string): string {\n  return name;\n}\n"
    assert any(c.symbol for c in chunker.chunk_file(ts, ".ts"))
    py = "def add(a, b):\n    return a + b\n"
    assert chunker.chunk_file(py, ".py")[0].symbol == "add"


def test_markdown_falls_back_to_line_windows():
    text = "\n".join(f"line {i}" for i in range(200))
    chunks = chunker.chunk_file(text, ".md")
    assert len(chunks) > 1
    assert chunks[0].symbol == ""
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 200


def test_unknown_extension_is_skipped():
    assert chunker.chunk_file("binaryish", ".bin") == []


def test_blankish_segment_is_dropped():
    assert chunker.chunk_file("\n\n\n\n", ".md") == []
