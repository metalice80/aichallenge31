from bisect import bisect_right
from dataclasses import dataclass, asdict
from hashlib import sha256
import re

from .documents import Document


@dataclass(frozen=True)
class Chunk:
    id: str
    path: str
    heading: str
    start_line: int
    end_line: int
    text: str
    hash: str

    source_type: str = "documentation"
    snapshot_sha: str = ""
    symbol: str = ""
    fallback: bool = False
    changed: bool = False

    def record(self):
        return asdict(self)


def split_document(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split at Markdown headings/paragraphs; hard limit is Unicode characters."""
    lines = doc.text.splitlines(keepends=True)
    offsets, position = [], 0
    sections, section_start, title, stack = [], 0, "", []
    fence = None
    for index, line in enumerate(lines):
        offsets.append(position)
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None and not fence_match else None
        setext = index + 1 < len(lines) and line.strip() and re.match(r"^ {0,3}(=+|-+)\s*$", lines[index + 1])
        if heading or (setext and fence is None):
            level = len(heading[1]) if heading else (1 if lines[index + 1].lstrip().startswith("=") else 2)
            name = heading[2] if heading else line.strip()
            if position > section_start:
                sections.append((section_start, position, title))
            stack = [(depth, text) for depth, text in stack if depth < level] + [(level, name)]
            title = " / ".join(text for _, text in stack)
            section_start = position
        position += len(line)
    sections.append((section_start, len(doc.text), title))
    chunks = []
    for start, end, heading in sections:
        while start < end:
            stop = min(start + size, end)
            if stop < end:
                boundaries = [m.end() + start for m in re.finditer(r"\n\s*\n", doc.text[start:stop])]
                boundaries = [b for b in boundaries if b - start >= size // 2]
                if boundaries:
                    stop = boundaries[-1]
                else:
                    newline = doc.text.rfind("\n", start + size // 2, stop)
                    if newline >= 0:
                        stop = newline + 1
            text = doc.text[start:stop]
            if text.strip():
                digest = sha256(text.encode()).hexdigest()
                chunk_id = sha256(f"{doc.path}\0{start}\0{stop}\0{digest}".encode()).hexdigest()
                chunks.append(Chunk(chunk_id, doc.path, heading, bisect_right(offsets, start), bisect_right(offsets, stop - 1), text, digest))
            if stop == end:
                break
            next_start = max(start + 1, stop - overlap)
            # Prefer a whole source line in the overlap; long lines may split mid-line.
            newline = doc.text.find("\n", next_start, stop - 1)
            start = newline + 1 if newline >= 0 else next_start
    return chunks
