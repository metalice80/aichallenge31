"""Bounded UTF-8 access through directory descriptors, never following symlinks."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid

from .config import AppError
from .documents import EXCLUDED, matches

SECRET = re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|(?im:(?:^|[,{])\s*[\"']?(?:api[_-]?key|password|token|access[_-]?token|client[_-]?secret|secret)[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,})")


def digest(raw):
    return sha256(raw).hexdigest()


def public_text(text):
    if SECRET.search(text):
        raise AppError("Secret-like content is excluded.")
    return text


def clean_path(name):
    if not isinstance(name, str) or not name or len(name) > 500 or "\\" in name or any(ord(c) < 32 for c in name):
        raise AppError("Invalid relative path.")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or p in EXCLUDED for p in parts):
        raise AppError("Excluded or escaping path.")
    if any(word in PurePosixPath(name).name.lower() for word in ("secret", "credential", ".env", ".lock", ".min.", ".generated.")):
        raise AppError("Sensitive/generated path excluded.")
    return name


class SafeRoot:
    def __init__(self, settings):
        self.settings, self.root = settings, settings.repo_path
        if not self.root.is_dir():
            raise AppError("Project root does not exist.")

    def allowed(self, name, write=False):
        clean_path(name)
        rules = self.settings.task_write_include if write else self.settings.task_read_include
        if not any(matches(name, rule) for rule in rules) or (write and not name.endswith(".md")):
            detail = (" Only Markdown documents may be changed: " + ", ".join(rules)
                      + ". Source code and code comments are read-only. Discover documentation with "
                        "search_text or list_files(glob='**/*.md'), then read_file the document."
                      if write else "")
            raise AppError("Path not in configured " + ("write" if write else "read") + " allowlist." + detail)

    @contextmanager
    def parent(self, name, create=False):
        clean_path(name)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in name.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, name.split("/")[-1]
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise AppError("Unsafe/unavailable path (including symlink): " + name) from exc
        finally:
            os.close(fd)

    def read(self, name, write=False):
        self.allowed(name, write)
        try:
            with self.parent(name) as (parent, leaf):
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise AppError("Only regular non-hardlinked files are supported.")
                    raw = stream.read(self.settings.task_max_file_bytes + 1)
                if len(raw) > self.settings.task_max_file_bytes:
                    raise AppError("File exceeds task_max_file_bytes.")
                if b"\0" in raw:
                    raise AppError("Binary file excluded.")
                public_text(raw.decode("utf-8"))
                return raw
        except FileNotFoundError:
            return None
        except UnicodeError as exc:
            raise AppError("Only UTF-8 text is supported.") from exc

    def replace(self, name, raw, expected):
        """Per-file atomic replace, not a multi-file transaction. Recheck at rename."""
        self.allowed(name, write=True)
        current = self.read(name, write=True)
        if (digest(current) if current is not None else None) != expected:
            raise AppError("conflict: " + name)
        with self.parent(name, create=True) as (parent, leaf):
            temp = ".outline-task-" + uuid.uuid4().hex
            try:
                mode = 0o644
                if current is not None:
                    mode = stat.S_IMODE(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode)
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                latest = self.read(name, write=True)
                if (digest(latest) if latest is not None else None) != expected:
                    raise AppError("conflict immediately before write: " + name)
                if expected is None:
                    # Atomic create-if-absent: never replace a concurrently created file.
                    os.link(temp, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink(temp, dir_fd=parent)
                else:
                    os.replace(temp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def undo_create(self, name, expected):
        # Only recovery may remove a file created by this run, never a model tool.
        self.allowed(name, write=True)
        raw = self.read(name, write=True)
        if raw is None or digest(raw) != expected:
            raise AppError("Recovery preserves changed file: " + name)
        with self.parent(name) as (parent, leaf):
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)

    def paths(self):
        result = []
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in EXCLUDED and not (Path(folder) / d).is_symlink())
            for leaf in sorted(files):
                path = Path(folder) / leaf
                name = path.relative_to(self.root).as_posix()
                try:
                    self.allowed(name)
                    if path.is_symlink() or not path.is_file():
                        continue
                except AppError:
                    continue
                result.append(name)
                if len(result) > self.settings.task_max_files:
                    return sorted(result[:-1]), True
        return sorted(result), False


def git(settings, *args, optional=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=", "-C", str(settings.repo_path), *args], capture_output=True, timeout=settings.git_timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError("Git task operation unavailable/timed out.") from exc
    if result.returncode:
        if optional:
            return None
        raise AppError("Git task operation failed: " + args[0])
    return result.stdout


def identity(settings):
    top = git(settings, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != settings.repo_path:
        raise AppError("repo_path must be the Git root.")
    info = settings.repo_path.stat()
    head = git(settings, "rev-parse", "--verify", "HEAD", optional=True)
    branch = git(settings, "symbolic-ref", "--short", "HEAD", optional=True)
    return {"root": str(settings.repo_path), "device": info.st_dev, "inode": info.st_ino,
            "head": head.decode().strip() if head else None, "branch": branch.decode().strip() if branch else None}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temp.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
