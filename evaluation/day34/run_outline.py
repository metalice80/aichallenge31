"""Read-only real Outline smoke. No apply flag, no branch/index/file mutations."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_files import git, identity
from outline_assistant.task_runner import run_task


OUT = Path(__file__).resolve().parent


def snapshot(settings):
    paths = git(settings, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    hashes = {}
    for path in sorted(set(paths) - {""}):
        target = settings.repo_path / path
        if target.is_symlink():
            hashes[path] = "symlink:" + str(target.readlink())
        elif target.is_file():
            hashes[path] = sha256(target.read_bytes()).hexdigest()
        else:
            hashes[path] = "absent/directory"
    return {"identity": identity(settings), "status": git(settings, "status", "--porcelain=v1", "--untracked-files=all").decode(),
            "files": len(hashes), "inventory_sha256": sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


async def main():
    base = load_settings()
    settings = replace(base, task_runs_dir=base.task_runs_dir / "outline-day34", publish_enabled=False)
    before = snapshot(settings)
    ollama = Ollama(settings)
    try:
        report = await run_task(settings, ollama, "Найди использования shallowEqual, объясни определение, вызовы, связи и ограничения поиска. Сам выбери и прочитай минимум три релевантных файла, используй текстовый и смысловой поиск. Создай отчёт в reports. Это dry-run: применение не разрешено.", progress=lambda text: print(text, flush=True))
    finally:
        ollama.close()
    after = snapshot(settings)
    directory = settings.task_runs_dir / report["run_id"]
    events = [json.loads(s) for s in (directory / "events.jsonl").read_text().splitlines()]
    result = {"run_id": report["run_id"], "status": report["status"], "directory": str(directory), "models": report["models"],
              "before": before, "after": after, "unchanged": before == after, "limitations": report["limitations"],
              "read_files": sorted({e["arguments"]["path"] for e in events if e.get("name") == "read_file" and e["kind"] == "tool" and e["result"].get("status") == "ok"}),
              "tool_sequence": [e["name"] for e in events if e["kind"] == "tool"]}
    (OUT / "outline-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    assert before == after, "Outline changed during dry-run"
    assert report["status"] == "prepared" and len(result["read_files"]) >= 3, result


if __name__ == "__main__":
    asyncio.run(main())
