"""Immutable Git objects, without checkout, hooks, textconv or PR execution."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from .config import AppError
from .documents import EXCLUDED, matches


def safe_path(path):
    try:
        path.encode("utf-8")
    except UnicodeError:
        return False
    parts = PurePosixPath(path).parts
    return bool(path) and not path.startswith("/") and not any(p in ("..", ".git") for p in parts) and "\x00" not in path and "\\" not in path


def source_kind(path, settings):
    if not safe_path(path):
        return None
    p = PurePosixPath(path)
    if any(part in EXCLUDED for part in p.parts) or any(word in p.name.lower() for word in (".env", "secret", "credential", ".generated.", ".min.", ".lock")) or p.suffix.lower() in (".pem", ".key", ".p12", ".map"):
        return None
    if path == "README.md" or (path.startswith("docs/") and path.endswith(".md")):
        return "documentation"
    if p.parts[0] not in ("app", "server", "shared", "plugins") or p.suffix not in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
        return None
    return "code" if any(matches(path, pattern) for pattern in settings.review_code_include) else None


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AppError("Кеш или модель заняты другим ревью. Повторите после завершения.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_hunks(patch):
    hunks, current = [], None
    old = new = 0
    for line in patch.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            old, new = int(match[1]), int(match[3])
            current = {"old_start": old, "old_count": int(match[2] or 1), "new_start": new, "new_count": int(match[4] or 1), "lines": []}
            hunks.append(current)
        elif current is not None and line[:1] in (" ", "+", "-"):
            kind = line[0]
            current["lines"].append({"kind": kind, "text": line[1:], "old_line": old if kind != "+" else None, "new_line": new if kind != "-" else None})
            old += kind != "+"
            new += kind != "-"
    return hunks


class GitSnapshot:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.review_git_dir / settings.github_repo.replace("/", "--")
        self.git_dir = self.root / "objects.git"
        self.allowed = set()
        self.trees = {}

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_PAGER": "cat"})
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", "-c", "protocol.ext.allow=never", "--git-dir", str(self.git_dir), *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.settings.review_git_timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AppError("Git снимка недоступен или превысил review_git_timeout.") from exc
        if check and result.returncode:
            # Do not expose remotes, credentials or attacker-controlled Git stderr.
            raise AppError(f"Git снимка: операция {args[0]} завершилась с кодом {result.returncode}.")
        return result

    def prepare(self, info):
        base, head = info["base_sha"], info["head_sha"]
        if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise AppError("Ожидался полный commit SHA.")
        with file_lock(self.root / "fetch.lock"):
            if not self.git_dir.exists():
                self.git_dir.mkdir(parents=True)
                self.git("init", "--bare")
            remote = self.settings.review_git_url or f"https://github.com/{self.settings.github_repo}.git"
            missing = [sha for sha in (base, head) if self.git("cat-file", "-e", sha + "^{commit}", check=False).returncode]
            if missing:
                self.git("fetch", "--no-tags", "--no-recurse-submodules", "--depth=64", remote, *missing)
            merge = self.git("merge-base", base, head, check=False)
            if merge.returncode and (self.git_dir / "shallow").exists():
                self.git("fetch", "--no-tags", "--no-recurse-submodules", "--unshallow", remote, base, head)
                merge = self.git("merge-base", base, head, check=False)
            if merge.returncode:
                raise AppError("Не удалось определить merge base: история недоступна или ветки не связаны.")
            merge_base = merge.stdout.decode().strip()
        self.allowed.update((base, head, merge_base))
        return {**info, "merge_base_sha": merge_base}

    def check_ref(self, ref):
        if ref not in self.allowed:
            raise AppError("SHA не принадлежит зафиксированному PR этой MCP-сессии.")

    def tree(self, ref):
        self.check_ref(ref)
        if ref not in self.trees:
            raw = self.git("ls-tree", "-r", "-z", "-l", ref).stdout
            entries = {}
            for record in raw.split(b"\0"):
                if not record:
                    continue
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid, size = header.decode().split()
                path = raw_path.decode("utf-8", errors="surrogateescape")
                entries[path] = {"mode": mode, "type": kind, "oid": oid, "bytes": int(size) if size != "-" else 0}
            self.trees[ref] = entries
        return self.trees[ref]

    def read(self, ref, path):
        self.check_ref(ref)
        if not safe_path(path):
            raise AppError("Недопустимый путь файла.")
        entry = self.tree(ref).get(path)
        result = {"path": path, "sha": ref, "text": "", "status": "missing", "total_lines": 0, "kind": source_kind(path, self.settings)}
        if entry is None:
            return result
        if entry["mode"] not in ("100644", "100755") or entry["type"] != "blob":
            return {**result, "status": "symlink_or_submodule"}
        if result["kind"] is None:
            return {**result, "status": "excluded"}
        if entry["bytes"] > self.settings.review_max_file_bytes:
            return {**result, "status": "too_large", "bytes": entry["bytes"]}
        raw = self.git("cat-file", "blob", entry["oid"]).stdout
        try:
            if b"\0" in raw:
                raise UnicodeError()
            text = raw.decode("utf-8")
        except UnicodeError:
            return {**result, "status": "binary"}
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}", text):
            return {**result, "status": "sensitive_content"}
        return {**result, "status": "ok", "text": text, "total_lines": len(text.splitlines())}

    def files(self, old, new):
        self.check_ref(old)
        self.check_ref(new)
        raw = self.git("diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames", old, new).stdout.split(b"\0")
        files, i = [], 0
        while i < len(raw) and raw[i]:
            status = raw[i].decode()
            path = raw[i + 1].decode("utf-8", errors="surrogateescape")
            i += 2
            previous = None
            if status[0] in "RC":
                previous = path
                path = raw[i].decode("utf-8", errors="surrogateescape")
                i += 1
            files.append({"path": path, "previous_path": previous, "status": {"A": "added", "D": "removed", "R": "renamed", "C": "copied"}.get(status[0], "modified")})
        return files

    def diff(self, info):
        old, new = info["merge_base_sha"], info["head_sha"]
        files = self.files(old, new)
        remaining = self.settings.review_max_diff_bytes
        output = []
        for index, file in enumerate(files):
            entry = {**file, "patch": "", "hunks": [], "incomplete": False, "reason": None}
            if index >= self.settings.review_max_files:
                output.append({**entry, "incomplete": True, "reason": "file_limit"})
                continue
            if not source_kind(file["path"], self.settings) or (file["previous_path"] and not source_kind(file["previous_path"], self.settings)):
                output.append({**entry, "incomplete": True, "reason": "outside_allowlist"})
                continue
            old_file = self.read(old, file["previous_path"] or file["path"])
            new_file = self.read(new, file["path"])
            invalid = [f["status"] for f in (old_file, new_file) if f["status"] not in ("ok", "missing")]
            if invalid:
                output.append({**entry, "incomplete": True, "reason": ",".join(invalid)})
                continue
            paths = list(dict.fromkeys((file["previous_path"] or file["path"], file["path"])))
            raw = self.git("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3", "--find-renames", old, new, "--", *[":(literal)" + p for p in paths]).stdout
            if len(raw) > remaining:
                output.append({**entry, "incomplete": True, "reason": "diff_bytes_limit"})
                continue
            remaining -= len(raw)
            patch = raw.decode("utf-8", errors="replace")
            hunks = parse_hunks(patch)
            output.append({**entry, "patch": patch, "hunks": hunks, "additions": sum(l["kind"] == "+" for h in hunks for l in h["lines"]), "deletions": sum(l["kind"] == "-" for h in hunks for l in h["lines"])})
        return {"base_sha": info["base_sha"], "head_sha": new, "merge_base_sha": old, "files": output, "incomplete": any(f["incomplete"] for f in output)}
