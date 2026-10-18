import argparse
import asyncio
import json
import shutil
import sys

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
    if args.command == "doctor":
        return await doctor(settings, ollama)
    if args.command == "docs":
        for document in read_documents(settings):
            print(document.path)
    elif args.command == "index":
        snapshot, stats = await asyncio.to_thread(Index(settings).update, ollama, progress, args.rebuild)
        print(json.dumps({**stats, "documents": len(snapshot.documents), "chunks": len(snapshot.chunks), "dimension": snapshot.metadata["dimension"]}, ensure_ascii=False))
    elif args.command == "ask":
        print(render(await ask(settings, ollama, args.question, progress)))
    else:
        print("Ассистент Outline. /help — описание проекта, /help <вопрос>, /exit.")
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            line = line.strip()
            if line == "/exit":
                break
            if line == "/help" or line.startswith("/help "):
                try:
                    print(render(await ask(settings, ollama, line[5:].strip() or OVERVIEW, progress)))
                    if line == "/help":
                        print("\n" + EXAMPLES)
                except (AppError, OSError) as exc:
                    print(f"Ошибка: {exc}", file=sys.stderr)
            elif line:
                print("Используйте /help, /help <вопрос> или /exit.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Локальный RAG-ассистент по Outline")
    parser.add_argument("--config", help="TOML-конфигурация (по умолчанию ./config.toml)")
    commands = parser.add_subparsers(dest="command")
    for name in ("doctor", "docs", "chat"):
        commands.add_parser(name)
    index = commands.add_parser("index")
    index.add_argument("--rebuild", action="store_true")
    question = commands.add_parser("ask")
    question.add_argument("question")
    args = parser.parse_args()
    ollama = None
    try:
        settings = load_settings(args.config)
        ollama = Ollama(settings)
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
