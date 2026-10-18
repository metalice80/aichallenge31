import argparse
import asyncio
import json
import shutil
import sys
import shlex

from .assistant import ask, render
from .config import AppError, load_settings
from .documents import read_documents
from .mcp_client import connect
from .ollama_client import Ollama
from .store import Index

OVERVIEW = "Что такое Outline и для кого он предназначен?"
EXAMPLES = "Примеры: /help Как включить HTTP-логи?\n/help Какие backend-сервисы нужны Outline?\n/help Как добавить перевод?\n/exit — завершить."


def progress(message):
    print(message, file=sys.stderr, flush=True)


async def doctor(settings, ollama):
    failed = []
    for name, check in (
        ("Git", lambda: shutil.which("git") or (_ for _ in ()).throw(AppError("Установите Git и добавьте его в PATH."))),
        ("Документы", lambda: [d.path for d in read_documents(settings)] or (_ for _ in ()).throw(AppError("Документы не найдены. Проверьте repo_path и include."))),
        ("Embedding-модель", lambda: ollama.identity(settings.embedding_model)),
        ("Модель ответов", lambda: ollama.identity(settings.chat_model)),
    ):
        try:
            print(f"OK {name}: {check()}")
        except (AppError, OSError) as exc:
            print(f"ОШИБКА {name}: {exc}")
            failed.append(name)
    try:
        async with connect(settings) as mcp:
            print("OK MCP/Git:", json.dumps(await mcp.call("get_git_context"), ensure_ascii=False))
            files = await mcp.call("list_project_files", {"limit": settings.file_limit})
            print(f"OK MCP/файлы: {len(files['files'])}; усечён: {files['truncated']}")
    except AppError as exc:
        print(f"ОШИБКА MCP: {exc}")
        failed.append("MCP")
    return 1 if failed else 0


async def run(args, settings, ollama):
    if args.command == "task":
        from .task_runner import run_task
        result = await run_task(settings, ollama, args.goal, args.base, args.apply, progress)
        print(f"Задача: {result['status']}; run_id: {result['run_id']}; артефакты: {settings.task_runs_dir / result['run_id']}")
        return 0 if result["status"] in ("prepared", "applied", "no_changes") else 1
    if args.command == "apply-run":
        from .task_apply import apply_run
        from .task_workspace import Workspace
        from .task_runner import event, render_report
        result = apply_run(settings, args.run_id)
        workspace = Workspace(settings, args.run_id)
        event(workspace.directory, kind="application", result=result)
        render_report(workspace)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in ("applied", "no_changes") and not result.get("error") else 1
    if args.command == "doctor":
        result = await doctor(settings, ollama)
        if args.review:
            from .review_ops import doctor_review
            result |= await doctor_review(settings)
        return result
    if args.command == "review-pr":
        from .review import run_review
        report = await run_review(settings, ollama, args.repo or settings.github_repo, args.number, args.output_dir, args.expected_head or "", progress)
        print(f"Ревью: {report['status']}; отчёт: {args.output_dir}/review.md")
        return 0 if report["status"] in ("complete", "partial") else 1
    if args.command == "publish-review":
        from .publishing import publish_review
        from pathlib import Path
        try:
            report = json.loads(Path(args.report).read_text())
        except (OSError, ValueError) as exc:
            raise AppError("Не удалось прочитать review.json.") from exc
        result = await asyncio.to_thread(publish_review, settings, args.repo or settings.github_repo, args.number, report, args.expected_head or "", args.run_url or "")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in ("published", "current", "closed") else 1
    if args.command == "cleanup-review":
        from .review_ops import cleanup_snapshots
        print(json.dumps({"apply": args.apply, "snapshots": cleanup_snapshots(settings, args.apply)}, ensure_ascii=False))
        return 0
    if args.command == "docs":
        for document in read_documents(settings):
            print(document.path)
    elif args.command == "index":
        snapshot, stats = await asyncio.to_thread(Index(settings).update, ollama, progress, args.rebuild)
        print(json.dumps({**stats, "documents": len(snapshot.documents), "chunks": len(snapshot.chunks), "dimension": snapshot.metadata["dimension"]}, ensure_ascii=False))
    elif args.command == "ask":
        print(render(await ask(settings, ollama, args.question, progress)))
    else:
        print("Ассистент Outline. /help — описание проекта, /help <вопрос>, /task [--apply] [--base REF] <цель>, /exit.")
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            line = line.strip()
            if line == "/exit":
                break
            if line == "/task" or line.startswith("/task "):
                from .task_runner import run_task
                try:
                    tokens = shlex.split(line[5:].strip())
                    apply, base = False, None
                    while tokens and tokens[0].startswith("--"):
                        option = tokens.pop(0)
                        if option == "--apply":
                            apply = True
                        elif option == "--base" and tokens:
                            base = tokens.pop(0)
                        else:
                            raise AppError("Используйте /task [--apply] [--base REF] <цель>.")
                    result = await run_task(settings, ollama, " ".join(tokens), base, apply, progress)
                    print(f"Задача: {result['status']}; run_id: {result['run_id']}")
                except (AppError, OSError, ValueError) as exc:
                    print(f"Ошибка: {exc}", file=sys.stderr)
            elif line == "/help" or line.startswith("/help "):
                try:
                    print(render(await ask(settings, ollama, line[5:].strip() or OVERVIEW, progress)))
                    if line == "/help":
                        print("\n" + EXAMPLES)
                except (AppError, OSError) as exc:
                    print(f"Ошибка: {exc}", file=sys.stderr)
            elif line:
                print("Используйте /help, /help <вопрос>, /task <цель> или /exit.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Локальный RAG-ассистент по Outline")
    parser.add_argument("--config", help="TOML-конфигурация (по умолчанию ./config.toml)")
    commands = parser.add_subparsers(dest="command")
    diagnostic = commands.add_parser("doctor")
    diagnostic.add_argument("--review", action="store_true")
    for name in ("docs", "chat"):
        commands.add_parser(name)
    task = commands.add_parser("task", help="Подготовить изменения Markdown через MCP/tool calling")
    task.add_argument("goal")
    task.add_argument("--base")
    task.add_argument("--apply", action="store_true")
    replay = commands.add_parser("apply-run", help="Применить сохранённый пакет без LLM")
    replay.add_argument("run_id")
    for name in ("review-pr", "publish-review"):
        review = commands.add_parser(name)
        review.add_argument("--repo")
        review.add_argument("--number", type=int, required=True)
        review.add_argument("--expected-head")
        if name == "review-pr":
            review.add_argument("--output-dir", required=True)
        else:
            review.add_argument("--report", required=True)
            review.add_argument("--run-url")
    cleanup = commands.add_parser("cleanup-review")
    cleanup.add_argument("--apply", action="store_true")
    index = commands.add_parser("index")
    index.add_argument("--rebuild", action="store_true")
    question = commands.add_parser("ask")
    question.add_argument("question")
    args = parser.parse_args()
    ollama = None
    try:
        settings = load_settings(args.config)
        ollama = None if args.command == "apply-run" else Ollama(settings)
        code = asyncio.run(run(args, settings, ollama))
    except (AppError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("\nОперация прервана. Опубликованный индекс сохранён.", file=sys.stderr)
        code = 130
    finally:
        if ollama:
            ollama.close()
    raise SystemExit(code)
