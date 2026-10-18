"""Deterministic schema/citation checks and safe report rendering."""
from datetime import datetime, timezone
from hashlib import sha256
import html
import importlib.metadata
import json
from pathlib import Path
import re
from urllib.parse import quote
import uuid
import os

from jsonschema import Draft202012Validator

from .config import AppError

FINDING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "category": {"enum": ["bug", "architecture", "recommendation"]},
        "severity": {"enum": ["high", "medium", "low"]},
        **{key: {"type": "string", "minLength": 1, "maxLength": 1800} for key in ("title", "description", "condition", "consequence", "recommendation", "introduced_by")},
        "path": {"type": "string", "minLength": 1}, "side": {"enum": ["old", "new"]},
        "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
        "sources": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string"}},
    },
    "required": ["category", "severity", "title", "description", "condition", "consequence", "recommendation", "introduced_by", "path", "side", "start_line", "end_line", "sources"],
}
REVIEW_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"summary": {"type": "string", "maxLength": 1500}, "findings": {"type": "array", "maxItems": 12, "items": FINDING_SCHEMA}}, "required": ["summary", "findings"]}


def assistant_identity():
    directory = Path(__file__).parent
    digest = sha256()
    for file in sorted(directory.glob("*.py")):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return {"version": importlib.metadata.version("outline-dev-assistant"), "code_sha256": digest.hexdigest()}


def validate_findings(raw, sources, files, info, allowed_ids):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AppError("Ревью: модель вернула неверный JSON.") from exc
    errors = list(Draft202012Validator(REVIEW_SCHEMA).iter_errors(data))
    if errors:
        raise AppError("Ревью: JSON не соответствует схеме.")
    accepted, rejected = [], []
    for finding in data["findings"]:
        reason = None
        side, path = finding["side"], finding["path"]
        start, end = finding["start_line"], finding["end_line"]
        sha = info["head_sha"] if side == "new" else info["merge_base_sha"]
        file = next((f for f in files if (f["path"] if side == "new" else f["previous_path"] or f["path"]) == path), None)
        ids = finding["sources"]
        if not file or end < start:
            reason = "неизвестный путь или диапазон"
        elif any(identifier not in allowed_ids or identifier not in sources for identifier in ids):
            reason = "неизвестный источник"
        elif not any(sources[i]["path"] == path and sources[i]["sha"] == sha and sources[i]["start_line"] <= start <= end <= sources[i]["end_line"] for i in ids):
            reason = "строки не подтверждены источником на указанной стороне"
        else:
            changed = [line["new_line" if side == "new" else "old_line"] for h in file["hunks"] for line in h["lines"] if line["kind"] == ("+" if side == "new" else "-")]
            if not any(start <= line <= end for line in changed):
                reason = "замечание не привязано к изменённым строкам"
        if reason:
            rejected.append({"title": finding["title"][:200], "reason": reason})
        else:
            accepted.append(finding)
    return data["summary"], accepted, rejected


def deduplicate(findings):
    result, seen = [], set()
    for finding in findings:
        key = (finding["category"], finding["path"], finding["side"], finding["start_line"], finding["end_line"], re.sub(r"\W+", "", finding["title"].lower()))
        if key not in seen:
            result.append(finding)
            seen.add(key)
    return sorted(result, key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f["severity"]], f["path"], f["start_line"]))


def safe_text(value):
    text = html.escape(str(value).encode("utf-8", errors="replace").decode(), quote=False).replace("@", "＠")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Model output is prose, never executable Markdown HTML/images/links.
    return re.sub(r"([\\`*_[\]{}()#!|>])", r"\\\1", text)


def code_link(repo, source):
    return f"https://github.com/{repo}/blob/{source['sha']}/{quote(source['path'], safe='/')}#L{source['start_line']}-L{source['end_line']}"


def render_review(report):
    info = report.get("pr", {})
    lines = [f"## Локальное AI-ревью — {safe_text(report['status'])}", "", f"Репозиторий: {safe_text(report['repo'])}; PR #{report['number']}.", f"Проверен head: `{info.get('head_sha', 'не получен')}`; base: `{info.get('base_sha', 'не получен')}`; merge base: `{info.get('merge_base_sha', 'не получен')}`.", f"Время: {report['created_at']}. Ассистент: {report['assistant']['version']}, код `{report['assistant']['code_sha256']}`.", "", "### Описание изменения", safe_text(report.get("summary", "Анализ не завершён.")), ""]
    for category, heading in (("bug", "Потенциальные баги"), ("architecture", "Архитектурные проблемы"), ("recommendation", "Рекомендации")):
        lines += [f"### {heading}", ""]
        findings = [f for f in report["findings"] if f["category"] == category]
        if not findings:
            lines.append("Подтверждённых замечаний в обработанной части не найдено." if report["status"] in ("complete", "partial") else "Нет достоверного результата анализа этой категории.")
        for finding in findings:
            sha = info["head_sha"] if finding["side"] == "new" else info["merge_base_sha"]
            location = {**finding, "sha": sha}
            lines += [f"- **{finding['severity']}: {safe_text(finding['title'])}** — [{safe_text(finding['path'])}:{finding['start_line']}–{finding['end_line']} ({finding['side']})]({code_link(report['repo'], location)})", f"  {safe_text(finding['description'])}", f"  Условие: {safe_text(finding['condition'])}. Последствие: {safe_text(finding['consequence'])}.", f"  Связь с PR: {safe_text(finding['introduced_by'])}.", f"  Исправление: {safe_text(finding['recommendation'])}."]
            lines.append("  Источники: " + ", ".join(f"[{i}]({code_link(report['repo'], report['sources'][i])})" for i in finding["sources"]))
        lines.append("")
    lines += ["### Покрытие", ""]
    for item in report["coverage"]:
        lines.append(f"- {safe_text(item['path'])}: **{item['status']}**" + (" — " + safe_text("; ".join(item["reasons"])) if item["reasons"] else ""))
    lines += ["", "### Ограничения", ""] + ["- " + safe_text(x) for x in report["limitations"]]
    lines += ["", "Источники проверены по SHA, путям и строкам; это не доказательство истинности замечаний и не гарантия безопасности PR.", "", "Модели: " + safe_text(json.dumps(report.get("models", {}), ensure_ascii=False))]
    return "\n".join(lines) + "\n"


def report_version(report):
    content = {k: v for k, v in report.items() if k not in ("result_version", "created_at", "publication")}
    return sha256(json.dumps(content, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def save_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report["result_version"] = report_version(report)
    for name, text in (("review.json", json.dumps(report, ensure_ascii=True, indent=2) + "\n"), ("review.md", render_review(report))):
        temporary = output / (name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(text)
        os.replace(temporary, output / name)


def new_report(repo, number):
    return {"schema_version": 1, "repo": repo, "number": number, "status": "failed", "created_at": datetime.now(timezone.utc).isoformat(), "assistant": assistant_identity(), "models": {}, "summary": "", "pr": {}, "findings": [], "sources": {}, "coverage": [], "limitations": [], "rejected": [], "batches": []}
