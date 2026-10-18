"""Real replay of the manual Outline usage goal; dry-run only, no fake models."""
import asyncio
import argparse
from dataclasses import replace
import json
from pathlib import Path

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_runner import run_task
from run_outline import snapshot


GOAL = "Найди все места использования API shallowEqual в проекте. Сам найди определение, импорты, реэкспорты и вызовы, включая возможные алиасы. Ищи по всему разрешённому коду, включая .ts и .tsx; при усечении результатов продолжи поиск. Прочитай контекст каждого найденного использования и проверь связи смысловым поиском. Подготовь в существующем docs/ARCHITECTURE.md краткий раздел с таблицей: файл и строки, вид использования, назначение. Сохрани английский язык и существующий текст документа. Укажи охват и ограничения поиска. Новые Markdown-файлы не создавай."


async def main(goal):
    base = load_settings()
    settings = replace(base, task_runs_dir=base.task_runs_dir / "usage-demo-fix", publish_enabled=False)
    before = snapshot(settings)
    ollama = Ollama(settings)
    try:
        report = await run_task(settings, ollama, goal, progress=lambda text: print(text, flush=True))
    finally:
        ollama.close()
    after = snapshot(settings)
    directory = settings.task_runs_dir / report["run_id"]
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    result = {"run_id": report["run_id"], "goal": goal, "status": report["status"], "directory": str(directory),
              "models": report.get("models"), "before": before, "after": after, "unchanged": before == after,
              "limitations": report["limitations"], "elapsed_seconds": report["elapsed_seconds"],
              "read_ranges": [{k: e["result"][k] for k in ("path", "start_line", "end_line")}
                              for e in events if e["kind"] == "tool" and e["name"] == "read_file" and e["result"].get("status") == "ok"],
              "tool_sequence": [e["name"] for e in events if e["kind"] == "tool"],
              "rag_stats": [e["result"].get("stats") for e in events if e["kind"] == "tool" and e["name"] == "search_context"],
              "changed_paths": list(report["changes"])}
    # Keep every attempt; a successful repeat must not erase a failed run.
    out = Path(__file__).parent / "usage-demo-fix"
    out.mkdir(exist_ok=True)
    (out / (report["run_id"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    assert before == after, "Outline changed during dry-run"
    assert report["status"] == "prepared", result
    assert result["changed_paths"] == ["docs/ARCHITECTURE.md"], result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", default=GOAL)
    asyncio.run(main(parser.parse_args().goal))
