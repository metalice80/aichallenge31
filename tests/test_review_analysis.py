import asyncio
from dataclasses import replace
import json

import pytest

from outline_assistant.config import AppError
from outline_assistant.review import run_review, build_messages
from outline_assistant.review_report import validate_findings, deduplicate, render_review
from review_helpers import setup_pr, api_server, review_settings


def finding(source="S1", **kwargs):
    return {"category": "bug", "severity": "high", "title": "Wrong addition", "description": "Subtracts the second argument", "condition": "b is nonzero", "consequence": "Wrong sum", "introduced_by": "PR replaces + with -", "recommendation": "Restore +", "path": "shared/utils/math.ts", "side": "new", "start_line": 2, "end_line": 2, "sources": [source], **kwargs}


def test_grounding_and_deduplication():
    info = {"head_sha": "a" * 40, "merge_base_sha": "b" * 40}
    sources = {"S1": {"path": "shared/utils/math.ts", "sha": info["head_sha"], "start_line": 1, "end_line": 3}}
    files = [{"path": "shared/utils/math.ts", "previous_path": None, "hunks": [{"lines": [{"kind": "+", "new_line": 2, "old_line": None}]}]}]
    valid = finding()
    invalid = [finding(path="invented.ts"), finding(start_line=100, end_line=100), finding(source="S99"), finding(side="old"), finding(start_line=1, end_line=1)]
    summary, accepted, rejected = validate_findings(json.dumps({"summary": "Test", "findings": [valid, valid, *invalid]}), sources, files, info, ["S1"])
    assert len(accepted) == 2 and len(rejected) == 5
    assert len(deduplicate(accepted)) == 1
    with pytest.raises(AppError):
        validate_findings("garbage", sources, files, info, ["S1"])


def test_full_pipeline_fake_models_real_stdio(settings, fake, tmp_path):
    info, _ = setup_pr(settings)
    calls = []
    def chat(messages, schema):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        assert payload["diff"]["lines"]
        head_sources = [s for s in payload["sources"] if s["sha"] == info["head"]["sha"] and s["path"] == "shared/utils/math.ts"]
        return json.dumps({"summary": "Subtraction introduced", "findings": [finding(head_sources[0]["id"])]})
    fake.chat = chat
    with api_server(info) as url:
        config = review_settings(settings, tmp_path, url)
        output = tmp_path / "output"
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, output))
        assert report["status"] == "complete", report
        assert len(report["findings"]) == 1
        assert report["pr"]["head_sha"] == info["head"]["sha"]
        assert (output / "review.json").exists() and (output / "review.md").exists()
        assert calls and any(s["sha"] == report["pr"]["merge_base_sha"] for s in calls[0]["sources"])
        assert "github.com/metalice80/outline_t/blob/" + info["head"]["sha"] in render_review(report)


@pytest.mark.parametrize("failure", ["json", "timeout"])
def test_failed_not_empty_success(settings, fake, tmp_path, failure):
    info, _ = setup_pr(settings)
    calls = []
    def chat(messages, schema):
        calls.append(1)
        if failure == "timeout":
            raise AppError("Таймаут Ollama")
        return "invalid"
    fake.chat = chat
    with api_server(info) as url:
        config = review_settings(settings, tmp_path, url)
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, tmp_path / "output"))
        assert report["status"] == "failed", report
        assert len(calls) == 2
        assert "Нет достоверного результата" in render_review(report)


def test_partial_and_stale_head(settings, fake, tmp_path):
    info, _ = setup_pr(settings)
    fake.chat = lambda *_: json.dumps({"summary": "No findings", "findings": []})
    with api_server(info) as url:
        config = replace(review_settings(settings, tmp_path, url), review_max_index_files=1)
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, tmp_path / "partial"))
        assert report["status"] == "partial"
        original_chat = fake.chat
        def update_head(*args):
            result = original_chat(*args)
            info["head"]["sha"] = info["base"]["sha"]
            return result
        fake.chat = update_head
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, tmp_path / "stale"))
        assert report["status"] == "stale", report


def test_empty_review_index_and_budget_limits(settings, fake, tmp_path):
    info, _ = setup_pr(settings)
    from conftest import git, commit
    git(settings, "switch", "feature")
    (settings.repo_path / "shared/utils/math.ts").write_text("export const payload = '" + "x" * 10000 + "';\n")
    commit(settings, "oversize changed block")
    info["head"]["sha"] = git(settings, "rev-parse", "HEAD")
    fake.chat = lambda *_: json.dumps({"summary": "none", "findings": []})
    with api_server(info) as url:
        config = review_settings(settings, tmp_path, url)
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, tmp_path / "oversize"))
        assert report["status"] == "failed"
        assert any("бюджет" in reason for c in report["coverage"] for reason in c["reasons"])
        config = replace(config, review_code_include=())
        # Remove docs from a new head so that the review index is actually empty.
        (settings.repo_path / "README.md").unlink()
        commit(settings, "no eligible source")
        info["head"]["sha"] = git(settings, "rev-parse", "HEAD")
        report = asyncio.run(run_review(config, fake, config.github_repo, 7, tmp_path / "empty"))
        assert report["rag"]["chunks"] == 0
        assert report["status"] != "complete"
        assert any("пуст" in limitation for limitation in report["limitations"])
