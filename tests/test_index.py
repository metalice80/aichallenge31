from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from outline_assistant.chunking import Chunk
from outline_assistant.config import AppError
from outline_assistant.search import search
from outline_assistant.store import Index, Snapshot, normalize


def test_known_cosine_and_invalid_vectors():
    chunks = [Chunk(str(i), "README.md", "h", 1, 2, "text", "hash") for i in range(3)]
    snap = Snapshot(Path("."), {}, normalize([[1, 0], [0, 1], [-1, 0]]), chunks, {})
    assert [h.chunk.id for h in search(snap, [2, 0], 2)] == ["0", "1"]
    assert search(snap, [2, 0], 2)[0].score == pytest.approx(1)
    for values in [[[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]], [[1], [1, 2]]]:
        with pytest.raises(AppError):
            normalize(values)
    with pytest.raises(AppError):
        search(snap, [1, 2, 3], 1)
    snap.chunks = []
    assert search(snap, [1, 0], 5) == []


def test_incremental_add_modify_remove_and_reuse(settings, fake):
    readme = settings.repo_path / "README.md"
    readme.write_text("# Outline\nOriginal\n")
    index = Index(settings)
    first, stats = index.update(fake)
    assert stats["embedded"] == 1
    calls = len(fake.calls)
    second, stats = index.update(fake)
    assert second.directory == first.directory
    assert len(fake.calls) == calls and stats["embedded"] == 0
    extra = settings.repo_path / "docs/new.md"
    extra.write_text("# New\nUntracked documentation\n")
    third, stats = index.update(fake)
    assert stats["embedded"] == 1 and stats["reused"] == 1
    readme.write_text("# Outline\nLocal modification\n")
    fourth, stats = index.update(fake)
    assert stats["embedded"] == 1 and stats["reused"] == 1
    assert "Local modification" in next(c.text for c in fourth.chunks if c.path == "README.md")
    extra.unlink()
    _, stats = index.update(fake)
    assert stats["removed"] == 1 and stats["embedded"] == 0
    (settings.repo_path / "code.ts").write_text("changed source")
    _, stats = index.update(fake)
    assert stats["embedded"] == 0
    readme.unlink()
    empty, stats = index.update(fake)
    assert not empty.chunks and empty.vectors.shape == (0, 4)


def test_failed_embedding_and_publish_preserve_snapshot(settings, fake):
    path = settings.repo_path / "README.md"
    path.write_text("Original")
    index = Index(settings)
    original, _ = index.update(fake)
    path.write_text("Changed")
    fake.fail = True
    with pytest.raises(RuntimeError):
        index.update(fake)
    assert index.load().directory == original.directory
    fake.fail = False
    with patch("outline_assistant.store.os.replace", side_effect=OSError("injected publication failure")):
        with pytest.raises(OSError):
            index.update(fake)
    assert index.load().directory == original.directory
    updated, _ = index.update(fake)
    assert updated.chunks[0].text == "Changed"
    assert updated.directory != original.directory


def test_config_model_and_chat_compatibility(settings, fake):
    (settings.repo_path / "README.md").write_text("# A\nDocumentation")
    Index(settings).update(fake)
    _, stats = Index(replace(settings, chat_model="different-chat")).update(fake)
    assert stats["embedded"] == 0
    _, stats = Index(replace(settings, chunk_chars=300)).update(fake)
    assert stats["embedded"] == 1
    fake.digest = "new-model-digest"
    _, stats = Index(replace(settings, chunk_chars=300)).update(fake)
    assert stats["embedded"] == 1


def test_checksum_and_rebuild(settings, fake):
    (settings.repo_path / "README.md").write_text("# A\nDocumentation")
    index = Index(settings)
    snapshot, _ = index.update(fake)
    (snapshot.directory / "vectors.npy").write_bytes(b"broken")
    with pytest.raises(AppError, match="повреждён"):
        index.load()
    assert index.update(fake, rebuild=True)[0].chunks


def test_documents_changed_during_embedding_not_published(settings, fake):
    path = settings.repo_path / "README.md"
    path.write_text("old")
    index = Index(settings)
    initial, _ = index.update(fake)
    path.write_text("new")
    original_embed = fake.embed
    def race(texts):
        path.write_text("concurrent edit")
        return original_embed(texts)
    fake.embed = race
    with pytest.raises(AppError, match="во время"):
        index.update(fake)
    assert index.load().directory == initial.directory


def test_single_writer(settings):
    index = Index(settings)
    with index.locked():
        with pytest.raises(AppError, match="Другой процесс"):
            with Index(settings).locked():
                pass


def test_add_after_initially_empty_index(settings, fake):
    index = Index(settings)
    snapshot, _ = index.update(fake)
    assert snapshot.vectors.shape == (0, 0)
    (settings.repo_path / "README.md").write_text("First document")
    snapshot, stats = index.update(fake)
    assert snapshot.vectors.shape == (1, 4) and stats["embedded"] == 1


def test_partial_batch_failure_and_dimension_change(settings, fake):
    (settings.repo_path / "README.md").write_text("Old content")
    index = Index(settings)
    original, _ = index.update(fake)
    (settings.repo_path / "README.md").write_text("new line\n" * 200)
    embed = fake.embed
    calls = 0
    def failing(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second batch failed")
        return embed(texts)
    fake.embed = failing
    with pytest.raises(RuntimeError):
        Index(replace(settings, batch_size=2)).update(fake)
    assert index.load().directory == original.directory
    fake.embed = lambda texts: [[1, 2, 3] for text in texts]
    updated, stats = index.update(fake)
    assert updated.vectors.shape[1] == 3
    assert stats["reused"] == 0
