"""Tree-sitter blocks with exact source lines; explicit bounded fallback."""
from dataclasses import replace
from hashlib import sha256
from pathlib import PurePosixPath

from tree_sitter import Language, Parser
import tree_sitter_typescript as ts
import tree_sitter_javascript as js

from .chunking import Chunk, split_document
from .documents import Document

PARSER_VERSION = "treesitter-v1"
BLOCKS = {"function_declaration", "generator_function_declaration", "method_definition", "class_declaration", "interface_declaration", "type_alias_declaration", "lexical_declaration", "export_statement", "import_statement", "expression_statement"}


def syntax_blocks(text, path):
    extension = PurePosixPath(path).suffix
    language = ts.language_tsx() if extension == ".tsx" else ts.language_typescript() if extension in (".ts", ".mts") else js.language()
    tree = Parser(Language(language)).parse(text.encode())
    if tree.root_node.has_error:
        return [], True
    blocks = []
    def visit(node, parent_name=""):
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode() if name_node else parent_name
        if node.type in BLOCKS:
            blocks.append({"start_line": node.start_point.row + 1, "end_line": node.end_point.row + (1 if node.end_point.column else 0), "symbol": name or node.type, "node_type": node.type})
        for child in node.named_children:
            visit(child, name)
    visit(tree.root_node)
    return blocks, False


def split_source(path, text, kind, sha, size, overlap, changed=False):
    if kind == "documentation":
        return [replace(c, snapshot_sha=sha, changed=changed, id=sha256(f"{sha}:{c.id}".encode()).hexdigest()) for c in split_document(Document(path, text, sha256(text.encode()).hexdigest()), size, overlap)]
    blocks, fallback = syntax_blocks(text, path)
    lines = text.splitlines(keepends=True)
    # Top-level partitions avoid duplicated class/method text in the index.
    boundaries = {0, len(lines)}
    for block in blocks:
        boundaries.update((block["start_line"] - 1, block["end_line"]))
    boundaries = sorted(boundaries)
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        candidates = [b for b in blocks if b["start_line"] <= left + 1 and b["end_line"] >= right]
        symbol = min(candidates, key=lambda b: (b["end_line"] - b["start_line"], b["symbol"] in BLOCKS))["symbol"] if candidates else "module"
        if fallback:
            symbol = "line-fallback"
        raw = "".join(lines[left:right])
        # Reuse the bounded splitter for exceptionally large syntax blocks.
        for chunk in split_document(Document(path, raw, ""), size, overlap):
            result.append(replace(chunk, id=sha256(f"{sha}:{path}:{left}:{chunk.id}".encode()).hexdigest(), start_line=left + chunk.start_line, end_line=left + chunk.end_line, heading=symbol, symbol=symbol, source_type="code", snapshot_sha=sha, fallback=fallback, changed=changed))
    return result


def affected_region(data, start, end):
    """Return the smallest complete containing syntax block, or a labelled line window."""
    lines = data["text"].splitlines(keepends=True)
    if not lines:
        return None
    start, end = max(1, min(start, len(lines))), max(1, min(end, len(lines)))
    blocks, fallback = syntax_blocks(data["text"], data["path"]) if data["kind"] == "code" else ([], False)
    containers = [b for b in blocks if b["start_line"] <= start and b["end_line"] >= end]
    if containers:
        # Prefer a function/method-sized block to merely the changed expression.
        functions = [b for b in containers if b["node_type"] in ("function_declaration", "generator_function_declaration", "method_definition", "class_declaration", "lexical_declaration")]
        block = min(functions or containers, key=lambda b: b["end_line"] - b["start_line"])
        start, end, symbol = block["start_line"], block["end_line"], block["symbol"]
    else:
        start, end, symbol = max(1, start - 8), min(len(lines), end + 8), "line-window"
    return {"path": data["path"], "sha": data["sha"], "start_line": start, "end_line": end, "text": "".join(lines[start - 1:end]), "source_type": data["kind"], "symbol": symbol, "fallback": fallback}
