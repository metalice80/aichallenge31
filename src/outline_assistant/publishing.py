"""Deterministic, explicitly enabled Issues Comments publication. No LLM calls."""
from datetime import datetime
import json
import os
from pathlib import Path
import re

from .config import AppError
from .github import GitHub, GitHubError
from .git_snapshot import file_lock
from .review_report import FINDING_SCHEMA, render_review, report_version, safe_text
from jsonschema import Draft202012Validator


def marker(repo, number):
    return f"<!-- local-ai-review:{repo}:{number} -->"


def metadata(body):
    match = re.search(r"<!-- local-ai-review-result:(\{[^\n]+\}) -->", body)
    if not match:
        return None
    try:
        return json.loads(match[1])
    except ValueError:
        return None


def validate_report(report, repo, number, expected_head=""):
    try:
        if report["schema_version"] != 1 or report["repo"] != repo or report["number"] != number:
            raise ValueError("репозиторий/номер/версия схемы")
        if report["status"] not in ("complete", "partial"):
            raise ValueError("публикуются только complete/partial")
        if report.get("result_version") != report_version(report):
            raise ValueError("контрольная сумма отчёта")
        pr = report["pr"]
        if pr["repo"] != repo or pr["number"] != number or pr["base_repo"] != repo or pr["head_repo"] != repo:
            raise ValueError("несовместимый PR")
        for key in ("head_sha", "base_sha", "merge_base_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}", pr[key]):
                raise ValueError("неверный SHA")
        if expected_head and pr["head_sha"] != expected_head:
            raise ValueError("expected head не совпадает с отчётом")
        datetime.fromisoformat(report["created_at"])
        for source in report["sources"].values():
            if source["sha"] not in (pr["head_sha"], pr["merge_base_sha"]) or not 1 <= source["start_line"] <= source["end_line"]:
                raise ValueError("источник отчёта")
        for finding in report["findings"]:
            if list(Draft202012Validator(FINDING_SCHEMA).iter_errors(finding)):
                raise ValueError("схема замечания")
            sha = pr["head_sha"] if finding["side"] == "new" else pr["merge_base_sha"]
            if any(i not in report["sources"] for i in finding["sources"]):
                raise ValueError("неизвестный источник замечания")
            evidence = [report["sources"][i] for i in finding["sources"]]
            if not any(s["sha"] == sha and s["path"] == finding["path"] and s["start_line"] <= finding["start_line"] <= finding["end_line"] <= s["end_line"] for s in evidence):
                raise ValueError("неподтверждённый диапазон замечания")
        # Rendering is also a structural check before any HTTP mutation.
        render_review(report)
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError(f"Невалидный отчёт для публикации: {exc}.") from exc


def comment_body(report, run_url="", max_bytes=60000):
    if run_url and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/\d+", run_url):
        raise AppError("run_url должен указывать на запуск GitHub Actions.")
    meta = {"head": report["pr"]["head_sha"], "version": report["result_version"], "created_at": report["created_at"]}
    prefix = marker(report["repo"], report["number"]) + "\n<!-- local-ai-review-result:" + json.dumps(meta, separators=(",", ":")) + " -->\n"
    full = prefix + render_review(report)
    suffix = f"\nПолный отчёт и артефакты: {run_url}\n" if run_url else "\nПолный отчёт: локальные review.json и review.md.\n"
    if len((full + suffix).encode()) <= max_bytes:
        return full + suffix
    # Retain high severity findings first; show limitations before listing findings.
    compact = prefix + f"## Локальное AI-ревью — {report['status']}\nПроверен SHA `{report['pr']['head_sha']}`.\nОтчёт сокращён; полное покрытие не подразумевается.\n"
    compact += "\n".join("- " + safe_text(x) for x in report["limitations"][:5]) + "\n"
    for item in report["findings"]:
        line = f"- {item['severity']}: {safe_text(item['title'])} — {safe_text(item['path'])}:{item['start_line']}. {safe_text(item['description'])}\n"
        if len((compact + line + suffix).encode()) > max_bytes:
            break
        compact += line
    if len((compact + suffix).encode()) > max_bytes:
        compact = compact.encode()[:max_bytes - len(suffix.encode())].decode(errors="ignore")
    return compact + suffix


def publish_review(settings, repo, number, report, expected_head="", run_url="", github=None):
    if not settings.publish_enabled:
        raise AppError("Публикация отключена: publish_enabled=false.")
    if repo != settings.github_repo or type(number) is not int or number <= 0:
        raise AppError("Недопустимый репозиторий или номер PR.")
    with file_lock(settings.review_index_dir / "publication-locks" / f"{repo.replace('/', '--')}-{number}.lock"):
        return _publish_review(settings, repo, number, report, expected_head, run_url, github)


def _publish_review(settings, repo, number, report, expected_head="", run_url="", github=None):
    if not settings.publish_enabled:
        raise AppError("Публикация отключена: publish_enabled=false. Включайте только в доверенной конфигурации runner после разрешения владельца.")
    validate_report(report, repo, number, expected_head)
    owned = github is None
    api = github or GitHub(settings)
    try:
        api.check_repo(repo)
        if not api.authenticated:
            raise AppError("Для публикации нужен GITHUB_TOKEN в окружении.")
        # Personal tokens expose /user; installation tokens used in Actions may not.
        try:
            actual_author = api.get("/user")["login"]
            if actual_author != settings.publication_author:
                raise AppError("Автор токена не совпадает с publication_author.")
        except GitHubError as exc:
            if exc.status != 403 or os.environ.get("GITHUB_ACTIONS") != "true" or settings.publication_author != "github-actions[bot]":
                raise
        info = api.pr(repo, number)
        if info["state"] != "open":
            return {"status": "closed", "action": "skipped"}
        if info["head_sha"] != report["pr"]["head_sha"]:
            return {"status": "stale", "action": "skipped"}
        comments, _ = api.pages(f"/repos/{repo}/issues/{number}/comments")
        own = [c for c in comments if c.get("user", {}).get("login") == settings.publication_author and marker(repo, number) in (c.get("body") or "").splitlines()]
        if len(own) > 1:
            raise AppError("Найдено несколько собственных review-комментариев; автоматическая публикация остановлена.")
        existing = own[0] if own else None
        if existing:
            previous = metadata(existing["body"])
            if previous and previous.get("head") == info["head_sha"]:
                if previous.get("version") == report["result_version"]:
                    return {"status": "current", "action": "unchanged", "comment_id": existing["id"]}
                if previous.get("created_at", "") > report["created_at"]:
                    return {"status": "stale", "action": "skipped", "comment_id": existing["id"]}
        # Re-read immediately before the write. GitHub has no CAS for PR head + issue comment.
        latest = api.pr(repo, number)
        if latest["state"] != "open" or latest["head_sha"] != info["head_sha"]:
            return {"status": "stale", "action": "skipped"}
        body = comment_body(report, run_url)
        if existing:
            response, _ = api.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": body})
            action = "updated"
        else:
            response, _ = api.request("POST", f"/repos/{repo}/issues/{number}/comments", body={"body": body})
            action = "created"
        if response.get("user", {}).get("login") != settings.publication_author:
            raise AppError("GitHub создал комментарий от неожиданного автора; проверьте токен. Повторная публикация остановлена.")
        return {"status": "published", "action": action, "comment_id": response["id"], "url": response.get("html_url"), "head_sha": info["head_sha"]}
    finally:
        if owned:
            api.close()
