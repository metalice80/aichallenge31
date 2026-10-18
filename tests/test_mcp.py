import asyncio
from dataclasses import replace

import pytest

from outline_assistant.config import AppError
from outline_assistant.mcp_client import connect
from outline_assistant.store import Index
from conftest import commit, git


async def context(settings, limit=80):
    async with connect(settings) as client:
        tools = await client.session.list_tools()
        assert {tool.name for tool in tools.tools} == {"get_git_context", "list_project_files"}
        return await client.call("get_git_context"), await client.call("list_project_files", {"limit": limit})


def test_stdio_unborn_branch_detached_and_local_changes(settings, fake):
    git_context, files = asyncio.run(context(settings))
    assert git_context == {"branch": "main", "commit": None, "detached": False, "unborn": True}
    assert files == {"files": [], "truncated": False}
    (settings.repo_path / "README.md").write_text("# Main\nMain documentation")
    (settings.repo_path / "docs/stable.md").write_text("Stable documentation")
    commit(settings)
    main_sha = git(settings, "rev-parse", "HEAD")
    index = Index(settings)
    index.update(fake)
    git(settings, "switch", "-c", "feature")
    (settings.repo_path / "README.md").write_text("# Feature\nDifferent documentation")
    commit(settings, "feature")
    _, stats = index.update(fake)
    assert stats["embedded"] == 1 and stats["reused"] == 1
    git_context, files = asyncio.run(context(settings, 1))
    assert git_context["branch"] == "feature" and git_context["commit"] != main_sha
    assert len(files["files"]) == 1 and files["truncated"]
    git(settings, "switch", "--detach", main_sha)
    _, stats = index.update(fake)
    assert stats["embedded"] == 1 and stats["reused"] == 1
    git_context, _ = asyncio.run(context(settings))
    assert git_context["detached"] and git_context["branch"] is None and git_context["commit"] == main_sha
    (settings.repo_path / "README.md").write_text("# Main\nUncommitted update")
    (settings.repo_path / "docs/local.md").write_text("New untracked documentation")
    _, stats = index.update(fake)
    assert stats["changed"] == 2
    git_context, files = asyncio.run(context(settings))
    assert git_context["commit"] == main_sha
    assert "docs/local.md" in files["files"]


def test_mcp_bad_root_and_limit(settings, tmp_path):
    with pytest.raises(AppError, match="MCP"):
        asyncio.run(context(replace(settings, repo_path=tmp_path / "missing")))
    async def invalid():
        async with connect(settings) as client:
            await client.call("list_project_files", {"limit": 501})
    with pytest.raises(AppError, match="MCP"):
        asyncio.run(invalid())
