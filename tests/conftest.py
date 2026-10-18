from dataclasses import replace
from hashlib import sha256
import subprocess

import pytest

from outline_assistant.config import Settings


@pytest.fixture
def settings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    return Settings(repo_path=repo, index_dir=tmp_path / "index", review_git_dir=tmp_path / "review-git", review_index_dir=tmp_path / "review-rag", task_runs_dir=tmp_path / "tasks", chunk_chars=256, overlap_chars=32)


class FakeOllama:
    """Deterministic test embeddings; never used by the real evaluation."""
    def __init__(self):
        self.calls = []
        self.fail = False
        self.digest = "test-model-v1"

    def identity(self, model):
        return {"name": model, "digest": self.digest}

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("injected embedding failure")
        self.calls.extend(texts)
        return [[v + 1 for v in sha256(text.encode()).digest()[:4]] for text in texts]


@pytest.fixture
def fake():
    return FakeOllama()


def git(settings, *args):
    return subprocess.run(["git", "-C", str(settings.repo_path), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(settings, message="fixture"):
    git(settings, "add", ".")
    git(settings, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message)
