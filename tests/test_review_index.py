from dataclasses import replace
from unittest.mock import patch

import pytest

from outline_assistant.code_chunks import split_source, affected_region
from outline_assistant.review_index import ReviewIndex
from outline_assistant.store import Index
from review_helpers import review_settings


def test_code_lines_symbols_and_fallback():
    text = "// intro\nexport function sum(a: number, b: number) {\n  return a + b;\n}\n"
    chunks = split_source("shared/math.ts", text, "code", "a" * 40, 256, 32)
    assert all(c.source_type == "code" and c.snapshot_sha == "a" * 40 for c in chunks)
    assert any(c.symbol == "sum" for c in chunks)
    for chunk in chunks:
        assert chunk.text in "".join(text.splitlines(keepends=True)[chunk.start_line - 1:chunk.end_line])
    region = affected_region({"path": "shared/math.ts", "sha": "a" * 40, "kind": "code", "text": text}, 3, 3)
    assert region["start_line"] == 2 and region["end_line"] == 4
    broken = split_source("shared/broken.ts", "function broken( {\n x();\n", "code", "a" * 40, 128, 10)
    assert broken and all(c.fallback for c in broken)


def test_review_cache_isolation_failure_and_model(settings, fake, tmp_path):
    config = review_settings(settings, tmp_path, "http://localhost")
    (settings.repo_path / "README.md").write_text("Existing help")
    help_snapshot, _ = Index(settings).update(fake)
    source = {"path": "shared/a.ts", "text": "export const a = 1;\n", "kind": "code"}
    doc = {"path": "README.md", "text": "# Architecture\nContract.\n", "kind": "documentation"}
    first = ReviewIndex(config, config.github_repo, 1, "a" * 40)
    snapshot, stats = first.update_sources(fake, [source, doc])
    assert {c.source_type for c in snapshot.chunks} == {"code", "documentation"}
    second = ReviewIndex(config, config.github_repo, 2, "b" * 40)
    reused, stats = second.update_sources(fake, [source, doc])
    assert stats["embedded"] == 0 and stats["reused"] == len(snapshot.chunks)
    assert reused.directory != snapshot.directory
    assert {c.snapshot_sha for c in reused.chunks} == {"b" * 40}
    changed = {**source, "text": "export const a = 2;\n"}
    with patch("outline_assistant.store.os.replace", side_effect=OSError("interrupt")):
        with pytest.raises(OSError):
            first.update_sources(fake, [changed, doc])
    assert first.load().directory == snapshot.directory
    _, stats = first.update_sources(fake, [changed, doc])
    assert stats["embedded"] == 0  # Prepared cache rows survive an interrupted publication.
    fake.digest = "model-v2"
    _, stats = first.update_sources(fake, [changed, doc])
    assert stats["embedded"] > 0
    assert Index(settings).load().directory == help_snapshot.directory


def test_cleanup_dry_run_and_active_lock(settings, tmp_path):
    import os, time
    from outline_assistant.review_ops import cleanup_snapshots
    from outline_assistant.git_snapshot import file_lock
    from outline_assistant.config import AppError
    old = settings.review_index_dir / "snapshots/repo/1-old"
    old.mkdir(parents=True)
    (old / "file").write_text("old")
    timestamp = time.time() - 30 * 86400
    os.utime(old, (timestamp, timestamp))
    assert cleanup_snapshots(settings) == [str(old)] and old.exists()
    with file_lock(settings.review_index_dir / "review.lock"):
        with pytest.raises(AppError):
            cleanup_snapshots(settings, apply=True)
    assert cleanup_snapshots(settings, apply=True) == [str(old)] and not old.exists()
