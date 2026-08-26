"""Split source files into embedding-sized chunks.

Strategy: for languages tree-sitter can parse, capture definition-like nodes
(functions, classes, impls, ...). A node that is too large is descended into
so an impl/class with many methods splits into per-method chunks. Lines not
covered by any captured node are grouped into residual line-window chunks.
Unsupported file types fall back to plain line windows.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_CHUNK_LINES = 250
MAX_CHUNK_BYTES = 8000
WINDOW_LINES = 80
WINDOW_OVERLAP = 15
MIN_CONTENT_LINES = 3

LANG_BY_EXT = {
    ".rs": "rust",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "tsx",
    ".py": "python",
    ".go": "go",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    # line-window only (no capture set defined)
    ".vue": "vue",
    ".md": "markdown",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sql": "sql",
    ".sh": "bash",
    ".proto": "proto",
    ".graphql": "graphql",
}

CAPTURE = {
    "rust": {
        "function_item", "struct_item", "enum_item", "trait_item",
        "impl_item", "macro_definition", "mod_item",
    },
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {
        "function_declaration", "generator_function_declaration",
        "class_declaration", "method_definition", "interface_declaration",
        "enum_declaration", "type_alias_declaration", "lexical_declaration",
        "export_statement",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "java": {
        "method_declaration", "class_declaration", "interface_declaration",
        "enum_declaration", "constructor_declaration",
    },
    "c": {"function_definition", "struct_specifier", "enum_specifier", "type_definition"},
    "cpp": {
        "function_definition", "struct_specifier", "enum_specifier",
        "class_specifier", "template_declaration", "namespace_definition",
        "type_definition",
    },
}
CAPTURE["tsx"] = CAPTURE["typescript"]
CAPTURE["javascript"] = CAPTURE["typescript"]

_parsers: dict[str, object] = {}


def _get_parser(lang: str):
    if lang not in _parsers:
        try:
            from tree_sitter_language_pack import get_parser
            _parsers[lang] = get_parser(lang)
        except Exception:
            _parsers[lang] = None
    return _parsers[lang]


@dataclass
class Chunk:
    start_line: int  # 1-based inclusive
    end_line: int
    symbol: str
    text: str


def _symbol_of(node, src: bytes) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return src[name.start_byte:name.end_byte].decode("utf-8", "replace")[:120]
    # first identifier-ish descendant, shallow scan
    for child in node.children:
        if "identifier" in child.type or child.type in ("type_identifier", "field_identifier"):
            return src[child.start_byte:child.end_byte].decode("utf-8", "replace")[:120]
        sub = child.child_by_field_name("name")
        if sub is not None:
            return src[sub.start_byte:sub.end_byte].decode("utf-8", "replace")[:120]
    return ""


def _node_chunks(node, src: bytes, capture: set, out: list[tuple[int, int, str]]):
    fits = (node.end_point[0] - node.start_point[0] + 1 <= MAX_CHUNK_LINES
            and node.end_byte - node.start_byte <= MAX_CHUNK_BYTES)
    if node.type in capture and fits:
        out.append((node.start_point[0], node.end_point[0], _symbol_of(node, src)))
        return
    for child in node.children:
        _node_chunks(child, src, capture, out)


def _windows(lines: list[str], first: int, last: int, out: list[Chunk]):
    """Emit line-window chunks covering lines[first..last] (0-based inclusive)."""
    i = first
    while i <= last:
        j = min(i + WINDOW_LINES - 1, last)
        seg = lines[i:j + 1]
        if sum(1 for ln in seg if ln.strip()) >= MIN_CONTENT_LINES:
            out.append(Chunk(i + 1, j + 1, "", "\n".join(seg)[:MAX_CHUNK_BYTES]))
        if j >= last:
            break
        i = j + 1 - WINDOW_OVERLAP
    return out


def chunk_file(text: str, ext: str) -> list[Chunk]:
    lang = LANG_BY_EXT.get(ext)
    if lang is None:
        return []
    lines = text.split("\n")
    capture = CAPTURE.get(lang)
    parser = _get_parser(lang) if capture else None
    if parser is None:
        out: list[Chunk] = []
        _windows(lines, 0, len(lines) - 1, out)
        return out

    src = text.encode("utf-8", "replace")
    tree = parser.parse(src)
    spans: list[tuple[int, int, str]] = []
    _node_chunks(tree.root_node, src, capture, spans)
    spans.sort()

    out = []
    covered_until = -1  # last 0-based line already covered
    for start, end, symbol in spans:
        if start > covered_until + 1:
            _windows(lines, covered_until + 1, start - 1, out)
        if end > covered_until:
            seg = "\n".join(lines[start:end + 1])
            out.append(Chunk(start + 1, end + 1, symbol, seg[:MAX_CHUNK_BYTES]))
            covered_until = end
    if covered_until + 1 <= len(lines) - 1:
        _windows(lines, covered_until + 1, len(lines) - 1, out)
    out.sort(key=lambda c: c.start_line)
    return out


def chunk_lang(ext: str) -> str:
    return LANG_BY_EXT.get(ext, "")
