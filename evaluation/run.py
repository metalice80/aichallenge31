"""Real local evaluation. No fake embeddings or answers. Run from the project root."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import time

from outline_assistant.assistant import ask, render
from outline_assistant.config import AppError, load_settings
from outline_assistant.documents import read_documents
from outline_assistant.mcp_client import connect
from outline_assistant.ollama_client import Ollama
from outline_assistant.search import search
from outline_assistant.store import Index

OUTPUT = Path("evaluation/results.json")


def progress(message):
    print(message, flush=True)


def save(data):
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


async def main():
    settings = load_settings()
    ollama = Ollama(settings)
    report = {"started": datetime.now(timezone.utc).isoformat(), "real_services": True, "cases": []}
    try:
        before = {"head": git(settings.repo_path, "rev-parse", "HEAD"), "branch": git(settings.repo_path, "symbolic-ref", "--short", "HEAD"), "status": git(settings.repo_path, "status", "--porcelain=v1"), "documents": {d.path: d.hash for d in read_documents(settings)}}
        report["outline_before"] = before
        report["models"] = {name: ollama.identity(name) for name in (settings.embedding_model, settings.chat_model)}
        snapshot, report["index_update"] = Index(settings).update(ollama, progress)
        report["index"] = {"documents": len(snapshot.documents), "chunks": len(snapshot.chunks), "dimension": snapshot.metadata["dimension"]}
        async with connect(settings) as mcp:
            report["mcp_git"] = await mcp.call("get_git_context")
            report["mcp_files"] = await mcp.call("list_project_files", {"limit": settings.file_limit})
        for case in json.loads(Path("evaluation/questions.json").read_text()):
            progress(f"Вопрос {case['id']}/25: {case['question']}")
            started = time.monotonic()
            hits = search(snapshot, ollama.embed([case["question"]])[0], settings.top_k)
            ranks = [i + 1 for i, hit in enumerate(hits) if hit.chunk.path == case["path"] and case["anchor"] in hit.chunk.text] if case["path"] else []
            result = {**case, "rank": min(ranks) if ranks else None, "retrieved": [{"path": h.chunk.path, "heading": h.chunk.heading, "lines": [h.chunk.start_line, h.chunk.end_line], "score": h.score} for h in hits]}
            if case.get("answer"):
                try:
                    answer = await ask(settings, ollama, case["question"], progress)
                    result["actual_answer"] = render(answer)
                    result["insufficient"] = answer.insufficient
                    result["update"] = answer.update
                except AppError as exc:
                    result["error"] = str(exc)
            result["seconds"] = round(time.monotonic() - started, 2)
            report["cases"].append(result)
            save(report)
        answerable = [c for c in report["cases"] if c["path"]]
        report["metrics"] = {"answerable": len(answerable), "hit_at_1": sum(c["rank"] == 1 for c in answerable) / len(answerable), "hit_at_5": sum(c["rank"] is not None for c in answerable) / len(answerable), "mrr_at_5": sum(1 / c["rank"] if c["rank"] else 0 for c in answerable) / len(answerable), "real_answers": sum("actual_answer" in c for c in report["cases"]), "answer_errors": sum("error" in c for c in report["cases"])}
        # All mutations and branch switches are confined to this disposable repository.
        with tempfile.TemporaryDirectory(prefix="outline-assistant-e2e-") as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            git(root, "init", "-b", "main")
            document = root / "README.md"
            document.write_text("# Учебный проект\n\nТекущий код стенда: ALPHA-731.\n")
            git(root, "add", "README.md")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
            temporary = replace(settings, repo_path=root, index_dir=Path(directory) / "index")
            report["temporary_repo"] = []
            async def check(label, expected, expected_embedded):
                answer = await ask(temporary, ollama, "Каков текущий код стенда?", progress)
                record = {"stage": label, "expected": expected, "actual_answer": render(answer), "update": answer.update, "passed": expected in answer.text and answer.update["embedded"] == expected_embedded}
                report["temporary_repo"].append(record)
                save(report)
                if not record["passed"]:
                    raise AppError(f"Реальная проверка обновления не прошла: {label}")
            await check("initial", "ALPHA-731", 1)
            await check("unchanged", "ALPHA-731", 0)
            document.write_text("# Учебный проект\n\nТекущий код стенда: BETA-942.\n")
            await check("uncommitted_edit", "BETA-942", 1)
            git(root, "switch", "-c", "feature")
            git(root, "add", "README.md")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "feature")
            await check("same_docs_new_branch", "BETA-942", 0)
            git(root, "switch", "--detach", "main")
            await check("detached_main", "ALPHA-731", 1)
        after = {"head": git(settings.repo_path, "rev-parse", "HEAD"), "branch": git(settings.repo_path, "symbolic-ref", "--short", "HEAD"), "status": git(settings.repo_path, "status", "--porcelain=v1"), "documents": {d.path: d.hash for d in read_documents(settings)}}
        report["outline_after"] = after
        report["outline_unchanged"] = before == after
        report["finished"] = datetime.now(timezone.utc).isoformat()
        save(report)
        progress(json.dumps(report["metrics"], ensure_ascii=False))
    except Exception as exc:
        report["fatal_error"] = str(exc)
        save(report)
        raise
    finally:
        ollama.close()


if __name__ == "__main__":
    asyncio.run(main())
