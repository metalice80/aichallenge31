"""Recheck already applied docs from the last run_real evaluation, without writes."""
import asyncio
import json
from pathlib import Path

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_runner import run_task
from run_real import inventory


async def main():
    out = Path(__file__).resolve().parent
    previous = json.loads((out / "results.json").read_text())
    config = load_settings(previous["docs_config"])
    before = inventory(config.repo_path)
    ollama = Ollama(config)
    try:
        report = await run_task(config, ollama, "Проверь соответствие существующей документации API текущему коду и тестам. Сам найди и прочитай минимум три связанных файла, используй смысловой поиск. Устраняй только фактические противоречия. Если документация уже верна, вызови preview и заверши no_changes; не дополняй описание и не меняй оформление.", progress=lambda text: print(text, flush=True))
    finally:
        ollama.close()
    directory = config.task_runs_dir / report["run_id"]
    events = [json.loads(s) for s in (directory / "events.jsonl").read_text().splitlines()]
    result = {"run_id": report["run_id"], "status": report["status"], "directory": str(directory), "config": previous["docs_config"],
              "models": report["models"], "changes": list(report["changes"]), "unchanged": before == inventory(config.repo_path), "limitations": report["limitations"],
              "read_files": sorted({s["path"] for s in report["sources"].values()}), "tool_sequence": [e["name"] for e in events if e["kind"] == "tool"]}
    (out / "repeat-final.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    assert result["status"] == "no_changes" and result["unchanged"] and not result["changes"], result


if __name__ == "__main__":
    asyncio.run(main())
