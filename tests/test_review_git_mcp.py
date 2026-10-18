import asyncio
from dataclasses import replace

import httpx
import pytest

from outline_assistant.config import AppError
from outline_assistant.git_snapshot import GitSnapshot, parse_hunks
from outline_assistant.github import GitHub
from outline_assistant.mcp_client import connect
from conftest import commit, git
from review_helpers import setup_pr, api_server, review_settings


def test_real_stdio_pins_head_and_merge_base(settings, tmp_path):
    info, merge = setup_pr(settings)
    with api_server(info) as url:
        configured = review_settings(settings, tmp_path, url)
        async def exercise():
            async with connect(configured, review=True) as mcp:
                pr = await mcp.call("get_pr_info", {"repo": configured.github_repo, "number": 7, "expected_head": info["head"]["sha"]})
                assert pr["head_sha"] == info["head"]["sha"]
                assert pr["merge_base_sha"] == merge != pr["base_sha"]
                files = await mcp.call("get_pr_files", {"repo": configured.github_repo, "number": 7})
                assert files["files"][0]["patch_available"] is False
                diff = await mcp.call("get_pr_diff", {"repo": configured.github_repo, "number": 7})
                assert len(diff["files"]) == 1
                assert "return a - b" in diff["files"][0]["patch"]
                old = await mcp.call("read_file_at_ref", {"repo": configured.github_repo, "ref": merge, "path": "shared/utils/math.ts"})
                assert "return a + b" in old["text"]
                listed = await mcp.call("list_snapshot_files", {"repo": configured.github_repo, "ref": pr["head_sha"]})
                assert "shared/utils/math.test.ts" in listed["files"]
                found = await mcp.call("search_snapshot", {"repo": configured.github_repo, "ref": pr["head_sha"], "terms": ["add("], "limit": 10})
                assert {h["path"] for h in found["hits"]} == {"shared/utils/math.ts", "shared/utils/math.test.ts"}
        asyncio.run(exercise())
        async def stale():
            async with connect(configured, review=True) as mcp:
                await mcp.call("get_pr_info", {"repo": configured.github_repo, "number": 7, "expected_head": "a" * 40})
        with pytest.raises(AppError, match="stale"):
            asyncio.run(stale())


def test_git_add_delete_rename_binary_paths_and_limits(settings, tmp_path):
    root = settings.repo_path
    (root / "shared").mkdir()
    for path in ("old.ts", "gone.ts"):
        (root / "shared" / path).write_text("export const oldValue = 1;\n")
    commit(settings)
    base = git(settings, "rev-parse", "HEAD")
    git(settings, "mv", "shared/old.ts", "shared/odd name\nquoted\".ts")
    (root / "shared/gone.ts").unlink()
    (root / "shared/new.ts").write_text("export const newValue = 2;\n")
    (root / "shared/binary.ts").write_bytes(b"\x00binary")
    (root / "shared/link.ts").symlink_to("/etc/passwd")
    commit(settings, "statuses")
    head = git(settings, "rev-parse", "HEAD")
    config = review_settings(settings, tmp_path, "http://localhost")
    snap = GitSnapshot(config)
    info = snap.prepare({"base_sha": base, "head_sha": head})
    diff = snap.diff(info)
    rows = {r["path"]: r for r in diff["files"]}
    assert {r["status"] for r in diff["files"]} >= {"added", "removed", "renamed"}
    assert rows['shared/odd name\nquoted".ts']["status"] == "renamed"
    assert rows["shared/binary.ts"]["reason"] == "binary"
    assert snap.read(head, "shared/link.ts")["status"] == "symlink_or_submodule"
    with pytest.raises(AppError):
        snap.read(head, "../etc/passwd")
    with pytest.raises(AppError):
        snap.read("b" * 40, "shared/new.ts")
    tiny = GitSnapshot(replace(config, review_max_diff_bytes=1))
    tiny.prepare(info)
    assert tiny.diff(info)["incomplete"]


def test_api_pagination_missing_patch_and_permissions(settings):
    calls = []
    def transport(request):
        calls.append(request)
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=[{"filename": "a.ts", "status": "modified", "additions": 5000, "deletions": 1}], headers={"Link": '<https://api.github.com/next>; rel="next"'})
        return httpx.Response(200, json=[{"filename": "b.ts", "status": "added", "additions": 1, "deletions": 0, "patch": "partial"}])
    api = GitHub(settings, httpx.MockTransport(transport))
    result = api.files(settings.github_repo, 1)
    assert len(result["files"]) == 2 and not result["patches_authoritative"]
    assert not result["files"][0]["patch_available"]
    assert [r.url.params["page"] for r in calls] == ["1", "2"]
    api.close()
    api = GitHub(settings, httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "denied"})))
    with pytest.raises(AppError, match="доступ"):
        api.pr(settings.github_repo, 1)
    api.close()


def test_secrets_not_read_and_api_url_validation(settings, tmp_path):
    from outline_assistant.config import AppError
    root = settings.repo_path
    (root / "shared").mkdir()
    (root / "shared/example.ts").write_text('const value = "ghp_' + 'X' * 36 + '";')
    commit(settings)
    sha = git(settings, "rev-parse", "HEAD")
    config = review_settings(settings, tmp_path, "http://localhost")
    snap = GitSnapshot(config)
    snap.prepare({"base_sha": sha, "head_sha": sha})
    assert snap.read(sha, "shared/example.ts")["status"] == "sensitive_content"
    for url in ("http://localhost.attacker.invalid", "https://token@example.com", "https://api.github.com?token=secret"):
        with pytest.raises(AppError):
            replace(settings, github_api_url=url)


def test_symbol_search_reaches_calls_after_import(settings, tmp_path):
    info, _ = setup_pr(settings)
    git(settings, "switch", "feature")
    (settings.repo_path / "shared/utils/math.test.ts").write_text("import { add } from './math';\n" + "\n" * 20 + "expect(add(1, 2)).toBe(3);\n")
    commit(settings, "distant caller")
    info["head"]["sha"] = git(settings, "rev-parse", "HEAD")
    with api_server(info) as url:
        configured = review_settings(settings, tmp_path, url)
        async def exercise():
            async with connect(configured, review=True) as mcp:
                pr = await mcp.call("get_pr_info", {"repo": configured.github_repo, "number": 7})
                found = await mcp.call("search_snapshot", {"repo": configured.github_repo, "ref": pr["head_sha"], "terms": ["add"], "limit": 10})
                caller = next(h for h in found["hits"] if h["path"].endswith("math.test.ts"))
                assert "expect(add(1, 2))" in caller["text"]
                assert caller["start_line"] > 1
        asyncio.run(exercise())
