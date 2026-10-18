"""Review diagnostics and explicit cleanup of generated snapshot indices."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sys
import time

from .config import AppError
from .git_snapshot import file_lock
from .mcp_client import connect
from .review_report import assistant_identity


async def doctor_review(settings):
    failed = False
    binary = Path(sys.executable).parent / "outline-assistant"
    print(f"Ассистент: {assistant_identity()}; executable: {binary}")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print("ОШИБКА: переустановите пакет в выбранный virtualenv.")
        failed = True
    for directory in (settings.review_git_dir, settings.review_index_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=directory):
                pass
            print(f"OK каталог: {directory}")
        except OSError:
            print(f"ОШИБКА записи каталога: {directory}")
            failed = True
    try:
        async with connect(settings, review=True) as mcp:
            result = await mcp.call("get_review_health")
            print(f"OK GitHub/MCP: repo={result['repo']}, default_branch={result['default_branch']}, authenticated={result['authenticated']}, permissions={result['permissions']}")
            if "runners_unverified" in result:
                print("НЕ ПРОВЕРЕНО runners: " + result["runners_unverified"])
            else:
                print("Runners:", result["runners"])
    except AppError as exc:
        print(f"ОШИБКА GitHub/MCP: {exc}")
        failed = True
    print(f"Публикация: {'включена' if settings.publish_enabled else 'отключена'}; ожидаемый автор: {settings.publication_author}")
    return int(failed)


def cleanup_snapshots(settings, apply=False):
    root = settings.review_index_dir / "snapshots"
    cutoff = time.time() - settings.review_keep_days * 86400
    removed = []
    with file_lock(settings.review_index_dir / "review.lock"):
        if not root.exists():
            return removed
        for repo in root.iterdir():
            if repo.is_symlink() or not repo.is_dir():
                continue
            for snapshot in repo.iterdir():
                if snapshot.is_symlink() or not snapshot.is_dir() or snapshot.stat().st_mtime >= cutoff:
                    continue
                if not snapshot.resolve().is_relative_to(root.resolve()):
                    raise AppError("Недопустимый путь очистки.")
                removed.append(str(snapshot))
                if apply:
                    shutil.rmtree(snapshot)
    return removed
