from dataclasses import replace
import pytest

from outline_assistant.chunking import split_document
from outline_assistant.config import AppError, load_settings
from outline_assistant.documents import Document, read_documents


def test_selection_and_symlinks(settings, tmp_path):
    root = settings.repo_path
    for name in ["README.md", "docs/root.md", "docs/sub/nested.md", "docs/node_modules/no.md", "docs/build/no.md", "app/no.md", "docs/code.ts", "OTHER.md"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("text")
    (tmp_path / "outside.md").write_text("outside")
    (root / "docs/link.md").symlink_to(tmp_path / "outside.md")
    (root / "docs/loop").symlink_to(root, target_is_directory=True)
    assert [d.path for d in read_documents(settings)] == ["README.md", "docs/root.md", "docs/sub/nested.md"]
    assert [d.path for d in read_documents(replace(settings, include=("docs/**/*.md",)))] == ["docs/root.md", "docs/sub/nested.md"]


def test_chunks_have_exact_lines_and_bounds():
    text = "# Title\n\nIntro.\n\n## Topic\n\n" + "Long paragraph sentence. " * 60 + "\n\n```python\n# not a heading\n```\n\nLast.\n"
    chunks = split_document(Document("docs/a.md", text, "hash"), 256, 32)
    covered = set()
    for chunk in chunks:
        assert 0 < len(chunk.text) <= 256
        start = text.find(chunk.text)
        assert start >= 0
        # Repeated words can occur many times: verify against the reported source range.
        lines = text.splitlines(keepends=True)
        assert chunk.text in "".join(lines[chunk.start_line - 1:chunk.end_line])
        covered.update(range(chunk.start_line, chunk.end_line + 1))
        assert "not a heading" not in chunk.heading
    assert "Title / Topic" in {c.heading for c in chunks}
    assert set(range(1, len(text.splitlines()) + 1)) <= covered


def test_setext_and_empty():
    assert split_document(Document("x", "\n  \n", "h"), 128, 10) == []
    chunks = split_document(Document("x", "Title\n=====\n\nText\n\nSub\n---\nBody\n", "h"), 128, 10)
    assert [c.heading for c in chunks] == ["Title", "Title / Sub"]
    assert chunks[-1].start_line == 6


def test_config_validation(settings, tmp_path):
    with pytest.raises(AppError):
        replace(settings, index_dir=settings.repo_path / ".index")
    with pytest.raises(AppError):
        replace(settings, overlap_chars=200)
    config = tmp_path / "config.toml"
    config.write_text('repo_path = "repo"\nindex_dir = "index"\ntop_k = 3\n')
    assert load_settings(str(config)).top_k == 3
    assert load_settings(str(config)).repo_path == settings.repo_path
