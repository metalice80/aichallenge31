from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
import os
from pathlib import Path

from .config import AppError, Settings

EXCLUDED = {".git", "node_modules", "vendor", "build", "dist", "coverage", ".next", ".venv", "__pycache__", ".cache", ".yarn", ".pnpm-store", "target", ".tox"}


@dataclass(frozen=True)
class Document:
    path: str
    text: str
    hash: str


def matches(path: str, pattern: str) -> bool:
    # '**/' also matches zero directories, unlike fnmatch by itself.
    return fnmatchcase(path, pattern) or ("**/" in pattern and matches(path, pattern.replace("**/", "", 1)))


def safe_files(root: Path):
    """Yield relative file names without following any symbolic links."""
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            if name in EXCLUDED:
                continue
            path = Path(folder) / name
            if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root):
                yield path.relative_to(root).as_posix()


def read_documents(settings: Settings) -> list[Document]:
    root = settings.repo_path
    if not root.is_dir():
        raise AppError(f"Нет каталога Outline: {root}. Исправьте repo_path в config.toml.")
    candidates = []
    readme = root / "README.md"
    if readme.is_file() and not readme.is_symlink():
        candidates.append("README.md")
    docs = root / "docs"
    if docs.is_dir() and not docs.is_symlink():
        candidates.extend("docs/" + name for name in safe_files(docs) if name.endswith(".md"))
    result = []
    for name in sorted(candidates):
        if not any(matches(name, rule) for rule in settings.include):
            continue
        try:
            raw = (root / name).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise AppError(f"Не удалось прочитать UTF-8 документ {name}: {exc}") from exc
        result.append(Document(name, text, sha256(raw).hexdigest()))
    return result
