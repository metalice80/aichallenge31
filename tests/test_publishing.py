from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from outline_assistant.config import AppError
from outline_assistant.github import GitHub
from outline_assistant.publishing import publish_review, marker, comment_body
from outline_assistant.review_report import new_report, report_version


def report_fixture():
    r = new_report("metalice80/outline_t", 7)
    r.update(status="complete", summary="@everyone <script>example</script>")
    r["pr"] = {"repo": r["repo"], "number": 7, "head_repo": r["repo"], "base_repo": r["repo"], "head_sha": "a" * 40, "base_sha": "b" * 40, "merge_base_sha": "b" * 40}
    r["result_version"] = report_version(r)
    return r


class APIState:
    def __init__(self):
        self.comments = []
        self.writes = []
        self.head = "a" * 40
        self.state = "open"
        self.pr_calls = 0
        self.change_on_second = False
        self.deny = False

    def handle(self, request):
        path = request.url.path
        if request.method in ("POST", "PATCH"):
            self.writes.append((request.method, path, json.loads(request.content)))
            if self.deny:
                return httpx.Response(403, json={})
            comment = {"id": 50, "user": {"login": "github-actions[bot]"}, "body": json.loads(request.content)["body"], "html_url": "https://github.com/metalice80/outline_t/pull/7#issuecomment-50"}
            self.comments = [c for c in self.comments if c["id"] != 50] + [comment]
            return httpx.Response(200, json=comment)
        if path == "/user":
            return httpx.Response(200, json={"login": "github-actions[bot]"})
        if "/comments" in path:
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(200, json=[{"id": 1, "user": {"login": "human"}, "body": marker("metalice80/outline_t", 7)}], headers={"Link": '<https://api.github.com/page2>; rel="next"'})
            return httpx.Response(200, json=self.comments)
        self.pr_calls += 1
        if self.change_on_second and self.pr_calls == 2:
            self.head = "c" * 40
        return httpx.Response(200, json={"number": 7, "state": self.state, "title": "test", "body": "", "draft": False, "user": {"login": "metalice80"}, "changed_files": 1, "head": {"sha": self.head, "repo": {"full_name": "metalice80/outline_t"}}, "base": {"sha": "b" * 40, "repo": {"full_name": "metalice80/outline_t"}}})


def test_create_update_idempotence_and_foreign_comments(settings, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")
    config = replace(settings, publish_enabled=True)
    state = APIState()
    api = GitHub(config, httpx.MockTransport(state.handle))
    report = report_fixture()
    result = publish_review(config, config.github_repo, 7, report, github=api)
    assert result["action"] == "created"
    assert state.writes[0][0] == "POST"
    assert "@everyone" not in state.writes[0][2]["body"]
    assert "<script>" not in state.writes[0][2]["body"]
    assert publish_review(config, config.github_repo, 7, report, github=api)["action"] == "unchanged"
    assert len(state.writes) == 1
    report["summary"] = "New result"
    report["result_version"] = report_version(report)
    assert publish_review(config, config.github_repo, 7, report, github=api)["action"] == "updated"
    assert state.writes[-1][1].endswith("/comments/50")
    assert not any(path.endswith("/comments/1") for _, path, _ in state.writes)
    api.close()


@pytest.mark.parametrize("condition", ["closed", "stale", "race", "denied", "disabled", "failed"])
def test_publication_boundaries(settings, monkeypatch, condition):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")
    config = replace(settings, publish_enabled=condition != "disabled")
    state = APIState()
    if condition == "closed": state.state = "closed"
    if condition == "stale": state.head = "c" * 40
    if condition == "race": state.change_on_second = True
    if condition == "denied": state.deny = True
    report = report_fixture()
    if condition == "failed":
        report["status"] = "failed"
        report["result_version"] = report_version(report)
    api = GitHub(config, httpx.MockTransport(state.handle))
    if condition in ("denied", "disabled", "failed"):
        with pytest.raises(AppError):
            publish_review(config, config.github_repo, 7, report, github=api)
    else:
        result = publish_review(config, config.github_repo, 7, report, github=api)
        assert result["status"] in ("stale", "closed")
    if condition != "denied":
        assert not state.writes
    api.close()


def test_comment_limit_and_run_link():
    report = report_fixture()
    report["limitations"] = ["Ограничение " * 300] * 10
    report["result_version"] = report_version(report)
    run_url = "https://github.com/metalice80/outline_t/actions/runs/123"
    body = comment_body(report, run_url, max_bytes=4000)
    assert len(body.encode()) <= 4000
    assert marker(report["repo"], 7) in body and run_url in body and "a" * 40 in body


def test_older_result_and_publication_lock(settings, monkeypatch):
    from outline_assistant.git_snapshot import file_lock
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")
    config = replace(settings, publish_enabled=True)
    state = APIState()
    api = GitHub(config, httpx.MockTransport(state.handle))
    report = report_fixture()
    newer = deepcopy(report)
    newer["created_at"] = "2099-01-01T00:00:00+00:00"
    newer["summary"] = "newer"
    newer["result_version"] = report_version(newer)
    state.comments = [{"id": 50, "user": {"login": "github-actions[bot]"}, "body": comment_body(newer)}]
    assert publish_review(config, config.github_repo, 7, report, github=api)["status"] == "stale"
    with file_lock(config.review_index_dir / "publication-locks/metalice80--outline_t-7.lock"):
        with pytest.raises(AppError, match="занят"):
            publish_review(config, config.github_repo, 7, report, github=api)
    assert not state.writes
    api.close()
