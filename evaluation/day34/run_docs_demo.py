"""Replay the user's docs goal with real Ollama/MCP; never apply to Outline."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_runner import run_task
from outline_assistant.task_workspace import Workspace
from run_outline import snapshot


GOAL = "Обнови существующую документацию по изменениям кода рабочей копии относительно HEAD. Сам посмотри diff, найди затронутое поведение и документы, прочитай связанный код и текущий текст, используй также смысловой поиск. Исправь только описание изменившегося поведения. Сохрани язык, структуру и несвязанные пользовательские правки. Новые Markdown-файлы не создавай."


def demo_checks(workspace):
    """Outline-specific acceptance checks live in evaluation, never the agent."""
    checks = {"only_readme": list(workspace.manifest["changes"]) == ["README.md"],
              "debugging_changed": False, "other_sections_preserved": False,
              "outdated_sentence_removed": False}
    if not checks["only_readme"]:
        return checks
    before = workspace.blob("originals", "README.md").read_text()
    after = workspace.blob("files", "README.md").read_text()
    marker, end = "## Debugging\n", "\n## Tests"
    if any(marker not in text or end not in text for text in (before, after)):
        return checks
    before_prefix, before_rest = before.split(marker, 1)
    after_prefix, after_rest = after.split(marker, 1)
    before_debug, before_suffix = before_rest.split(end, 1)
    after_debug, after_suffix = after_rest.split(end, 1)
    checks.update(debugging_changed=before_debug != after_debug,
                  other_sections_preserved=(before_prefix, before_suffix) == (after_prefix, after_suffix),
                  outdated_sentence_removed="HTTP logging is disabled by default, but can be enabled by setting the `DEBUG=http` environment variable." not in after_debug)
    return checks


async def main():
    base = load_settings()
    settings = replace(base, task_runs_dir=base.task_runs_dir / "docs-demo-fix", publish_enabled=False)
    before = snapshot(settings)
    ollama = Ollama(settings)
    try:
        report = await run_task(settings, ollama, GOAL, progress=lambda text: print(text, flush=True))
    finally:
        ollama.close()
    after = snapshot(settings)
    directory = settings.task_runs_dir / report["run_id"]
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    actual = [e for e in events if e["kind"] == "tool"]
    result = {"run_id": report["run_id"], "goal": GOAL, "status": report["status"], "directory": str(directory),
              "models": report.get("models"), "before": before, "after": after, "unchanged": before == after,
              "limitations": report["limitations"], "elapsed_seconds": report["elapsed_seconds"],
              "read_ranges": [{k: e["result"][k] for k in ("path", "start_line", "end_line")}
                              for e in actual if e["name"] == "read_file" and e["result"].get("status") == "ok"],
              "tool_sequence": [e["name"] for e in actual],
              "rag_stats": [e["result"].get("stats") for e in actual if e["name"] == "search_context"],
              "changed_paths": list(report["changes"]),
              "demo_checks": demo_checks(Workspace(settings, report["run_id"])),
              "patch": (directory / "changes.patch").read_text()}
    out = Path(__file__).parent / "docs-demo-fix"
    out.mkdir(exist_ok=True)
    (out / (report["run_id"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("run_id", "status", "unchanged", "elapsed_seconds", "changed_paths")}), flush=True)
    assert before == after, "Outline changed during dry-run"
    assert result["status"] == "prepared", result["limitations"]
    assert result["changed_paths"] == ["README.md"]
    assert len({r["path"] for r in result["read_ranges"]}) >= 3
    assert result["rag_stats"], "No real semantic search"
    assert all(result["demo_checks"].values()), result["demo_checks"]
    # Success also requires MANUAL review of the saved patch; status is not enough.


if __name__ == "__main__":
    asyncio.run(main())
