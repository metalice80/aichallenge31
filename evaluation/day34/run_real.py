"""Real Qwen3/BGE-M3/MCP evaluation. Writes only an isolated temporary project.

Run from project root: .venv/bin/python evaluation/day34/run_real.py
Artifacts remain in .task-runs; JSON records the temporary project for inspection.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import sys

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_runner import run_task, render_report
from outline_assistant.task_apply import apply_run
from outline_assistant.task_workspace import Workspace

OUT = Path(__file__).resolve().parent


def git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def inventory(root):
    from hashlib import sha256
    return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


async def main():
    base = load_settings()
    root = Path(tempfile.mkdtemp(prefix="outline-file-task-real-")) / "project"
    root.mkdir()
    git(root, "init", "-b", "demo")
    files = {
        "lib/paging/normalize.ts": "export function normalizePageSize(value?: number): number {\n  if (value === undefined) return 10;\n  return Math.max(1, Math.min(value, 50));\n}\n",
        "client/table.ts": "import { normalizePageSize as pageSize } from '../lib/paging/normalize';\nexport function tableRequest(size?: number) {\n  return { limit: pageSize(size) };\n}\n",
        "lib/public.ts": "export { normalizePageSize } from './paging/normalize';\n",
        "checks/paging.test.ts": "import { normalizePageSize } from '../lib/public';\nexpect(normalizePageSize()).toBe(10);\nexpect(normalizePageSize(250)).toBe(50);\nexpect(normalizePageSize(-2)).toBe(1);\n",
        "docs/pagination.md": "# Pagination API\n\nThe default page size is 10. Values are clamped between 1 and 50.\n\n## Client\n\nThe table client sends the normalized value as the limit parameter.\n\n## Local notes\n\nPreserve this unrelated user section.\n",
        "README.md": "# Table service\n\nPaging helpers and a table client. See [Pagination API](docs/pagination.md).\n",
    }
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(root, "add", ".")
    git(root, "-c", "user.name=Local evaluation", "-c", "user.email=eval@example.invalid", "commit", "-m", "Initial API fixture")
    config = replace(base, repo_path=root, task_runs_dir=base.task_runs_dir / "real-day34",
                     index_dir=root.parent / "help-index", review_git_dir=root.parent / "pr-git", review_index_dir=root.parent / "pr-index", publish_enabled=False)
    config_path = root.parent / "fixture.toml"
    def write_config(url):
        values = {k: str(getattr(config, k)) for k in ("repo_path", "index_dir", "review_git_dir", "review_index_dir", "task_runs_dir")}
        values["ollama_url"] = url
        config_path.write_text("\n".join(k + " = " + json.dumps(v) for k, v in values.items()) + "\n")
    write_config(config.ollama_url)
    result = {"started": datetime.now(timezone.utc).isoformat(), "root": str(root), "real_models": True,
              "real_mcp_stdio": True, "github": "not used", "config": str(config_path), "runs": []}
    def save():
        (OUT / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    ollama = Ollama(config)
    try:
        for name, goal in [
            ("usage_report", "Найди использования API normalizePageSize, объясни связи, алиасы и тесты, создай Markdown-отчёт в reports с таблицей путей и строк и ограничениями поиска. Выбери файлы самостоятельно, используй также смысловой поиск."),
            ("docs_update", "Обнови существующую документацию API по изменениям кода рабочей копии. Сам найди документы, проверь изменённый код и тесты, используй смысловой поиск. Сохрани язык, структуру и несвязанные пользовательские заметки."),
        ]:
            if name == "docs_update":
                # Independent scenario: a fresh project, not the output corpus of A.
                # Prior attempts with historical reports remain recorded separately.
                root = Path(tempfile.mkdtemp(prefix="outline-docs-task-real-")) / "project"
                root.mkdir()
                git(root, "init", "-b", "demo")
                for path, text in files.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                git(root, "add", ".")
                git(root, "-c", "user.name=Local evaluation", "-c", "user.email=eval@example.invalid", "commit", "-m", "Initial independent docs fixture")
                config = replace(config, repo_path=root, index_dir=root.parent / "help-index", review_git_dir=root.parent / "pr-git", review_index_dir=root.parent / "pr-index")
                config_path = root.parent / "fixture.toml"
                write_config(config.ollama_url)
                result["docs_root"], result["docs_config"] = str(root), str(config_path)
                for path in ("lib/paging/normalize.ts", "checks/paging.test.ts"):
                    p = root / path
                    p.write_text(p.read_text().replace("return 10", "return 25").replace("value, 50", "value, 100").replace("toBe(10)", "toBe(25)").replace("toBe(50)", "toBe(100)"))
                # Test both staged and unstaged changes; notes are pre-existing user edits.
                git(root, "add", "lib/paging/normalize.ts")
                p = root / "docs/pagination.md"
                p.write_text(p.read_text() + "\nUser's pending note must survive.\n")
            before = inventory(root)
            print("SCENARIO", name, flush=True)
            report = await run_task(config, ollama, goal, progress=lambda msg: print(msg, flush=True))
            events_path = config.task_runs_dir / report["run_id"] / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            row = {"name": name, "root": str(root), "config": str(config_path), "run_id": report["run_id"], "directory": str(events_path.parent), "status": report["status"],
                   "dry_run_unchanged": before == inventory(root), "read_files": sorted({e["arguments"]["path"] for e in events if e.get("name") == "read_file" and e["kind"] == "tool" and e["result"].get("status") == "ok"}),
                   "tool_sequence": [e["name"] for e in events if e["kind"] == "tool"], "limitations": report["limitations"]}
            result["runs"].append(row)
            save()
            if report["status"] != "prepared":
                continue
            if name == "docs_update":
                code = root / "lib/paging/normalize.ts"
                current = code.read_bytes()
                code.write_bytes(current + b"// concurrent user change\n")
                row["source_conflict"] = apply_run(config, report["run_id"])
                assert "User's pending note" in (root / "docs/pagination.md").read_text()
                code.write_bytes(current)
            row["apply"] = apply_run(config, report["run_id"])
            row["replay_without_llm"] = apply_run(config, report["run_id"])
            write_config("http://127.0.0.1:9")
            replay = subprocess.run([str(Path(sys.executable).parent / "outline-assistant"), "--config", str(config_path), "apply-run", report["run_id"]], capture_output=True, text=True)
            row["cli_replay_unavailable_ollama"] = {"exit_code": replay.returncode, "stdout": replay.stdout.strip(), "stderr": replay.stderr.strip()}
            write_config(config.ollama_url)
            render_report(Workspace(config, report["run_id"]))
            after = inventory(root)
            row["changed_paths"] = [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
            row["sources_unchanged"] = all(before[p] == after[p] for p in before if p.endswith(".ts"))
            if name == "docs_update":
                text = (root / "docs/pagination.md").read_text()
                row["meaning_updated"] = "25" in text and "100" in text and "User's pending note" in text and "Preserve this unrelated" in text
            save()
        print("SCENARIO repeat_docs", flush=True)
        before = inventory(root)
        repeated = await run_task(config, ollama, "Проверь документацию API по текущим изменениям кода. Если она уже соответствует коду и тестам, оставь её без изменений. Сам выбери и прочитай файлы, используй смысловой поиск.", progress=lambda msg: print(msg, flush=True))
        result["repeat_docs"] = {"run_id": repeated["run_id"], "status": repeated["status"], "changes": list(repeated["changes"]), "project_unchanged": before == inventory(root), "limitations": repeated["limitations"]}
        # Explicit CLI --apply against an intentionally stale fixture document.
        p = root / "docs/pagination.md"
        p.write_text(p.read_text().replace("size is 25", "size is 18"))
        before_runs = {p.name for p in config.task_runs_dir.iterdir() if p.is_dir()}
        applied = subprocess.run([str(Path(sys.executable).parent / "outline-assistant"), "--config", str(config_path), "task", "--apply", "Обнови документацию API по изменениям кода. Сам найди и прочитай код, тесты и документацию, используй смысловой поиск, сохрани пользовательские заметки."], capture_output=True, text=True)
        created = sorted(p.name for p in config.task_runs_dir.iterdir() if p.is_dir() and p.name not in before_runs)
        result["cli_apply"] = {"exit_code": applied.returncode, "stdout": applied.stdout.strip(), "stderr": applied.stderr.strip(), "run_ids": created, "meaning_updated": "size is 25" in p.read_text() and "User's pending note" in p.read_text()}
    finally:
        ollama.close()
        result["finished"] = datetime.now(timezone.utc).isoformat()
        save()
    assert all(r["status"] == "prepared" and len(r["read_files"]) >= 3 and "search_context" in r["tool_sequence"] and r.get("apply", {}).get("status") == "applied" and r.get("cli_replay_unavailable_ollama", {}).get("exit_code") == 0 for r in result["runs"]), result
    assert result["repeat_docs"]["status"] == "no_changes", result["repeat_docs"]
    assert result["runs"][1].get("meaning_updated") is True, result["runs"][1]
    assert result["cli_apply"]["exit_code"] == 0 and result["cli_apply"]["meaning_updated"], result["cli_apply"]


if __name__ == "__main__":
    asyncio.run(main())
