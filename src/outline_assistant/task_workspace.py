"""Session overlay and provenance. Only task MCP exposes this interface."""
from dataclasses import asdict
from datetime import datetime, timezone
import difflib
import json
import posixpath
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit
import uuid

from .config import AppError
from .documents import matches
from .review_index import ReviewIndex
from .review_report import assistant_identity
from .search import search
from .store import normalize
from .task_files import SafeRoot, clean_path, digest, git, identity, public_text, write_json


def unified_patch(before, after, fromfile, tofile):
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=fromfile, tofile=tofile)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def run_path(settings, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise AppError("Invalid run_id (expected 32 hex characters).")
    path = settings.task_runs_dir / run_id
    if path.is_symlink() or path.resolve().parent != settings.task_runs_dir:
        raise AppError("Unsafe run directory.")
    return path


def create_run(settings, goal, base=None):
    if not goal.strip() or len(goal.encode()) > 4000:
        raise AppError("Goal must contain 1–4000 UTF-8 bytes.")
    public_text(goal)
    repo = identity(settings)
    comparison = repo["head"]
    if base:
        if len(base) > 200 or base.startswith("-"):
            raise AppError("Invalid base ref.")
        comparison = git(settings, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    run_id = uuid.uuid4().hex
    directory = run_path(settings, run_id)
    directory.mkdir(parents=True)
    # State contains paths/status only; no Git remotes, env or auth tokens.
    status = git(settings, "status", "--porcelain=v1", "--untracked-files=all").decode("utf-8", "replace")
    manifest = {"schema_version": 1, "run_id": run_id, "goal": goal, "repository": repo,
        "comparison": {"base_ref": base, "base_sha": comparison, "head": repo["head"],
                       "mode": "explicit-base-to-working-tree" if base else "HEAD-to-working-tree", "includes": "staged + unstaged + untracked"},
        "initial_git_status": status[:12000], "git_status_truncated": len(status) > 12000,
        "created_at": datetime.now(timezone.utc).isoformat(), "assistant": assistant_identity(),
        "settings": {k: v for k, v in asdict(settings).items() if k.startswith("task_") and k != "task_runs_dir"},
        "models": {}, "status": "running", "inputs": {}, "sources": {}, "changes": {},
        "read_bytes": 0, "revision": 0, "preview_revision": -1, "limitations": [], "summary": ""}
    write_json(directory / "manifest.json", manifest)
    return run_id


class Workspace:
    def __init__(self, settings, run_id):
        self.settings, self.run_id = settings, run_id
        self.directory = run_path(settings, run_id)
        self.manifest = json.loads((self.directory / "manifest.json").read_text())
        self.files = SafeRoot(settings)
        if self.manifest["repository"]["root"] != str(settings.repo_path):
            raise AppError("Run belongs to a different project.")

    def save(self):
        write_json(self.directory / "manifest.json", self.manifest)

    def blob(self, folder, path):
        # Artifact names are derived hashes, not model-controlled relative paths.
        return self.directory / folder / digest(path.encode())

    def input(self, path, raw):
        hashed = digest(raw) if raw is not None else None
        if path in self.manifest["inputs"] and self.manifest["inputs"][path] != hashed:
            raise AppError("conflict: project source changed during task: " + path)
        self.manifest["inputs"][path] = hashed

    def content(self, path):
        self.files.allowed(path)
        raw = self.files.read(path)
        if path in self.manifest["changes"]:
            change = self.manifest["changes"][path]
            if (digest(raw) if raw is not None else None) != change["before_hash"]:
                raise AppError("conflict: target changed during task: " + path)
            raw = self.blob("files", path).read_bytes()
            if digest(raw) != change["after_hash"]:
                raise AppError("Overlay checksum mismatch.")
        return raw

    def listing(self):
        paths, truncated = self.files.paths()
        return sorted(set(paths) | set(self.manifest["changes"])), truncated

    def list_files(self, glob="**/*", offset=0, limit=50):
        self.validate_glob(glob)
        if offset < 0 or not 1 <= limit <= 100:
            raise AppError("offset >= 0, limit 1–100 required.")
        paths, truncated = self.listing()
        paths = [p for p in paths if matches(p, glob)]
        return {"files": paths[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(paths) else None,
                "total_visible": len(paths), "truncated": truncated or offset + limit < len(paths)}

    @staticmethod
    def validate_glob(glob):
        if "{" in glob or "}" in glob:
            raise AppError("Brace expansion is not supported in glob. Use glob='**/*' to include TS and TSX, or separate calls for '**/*.ts' and '**/*.tsx'.")

    def read_file(self, path, start_line=1, end_line=120):
        if start_line < 1 or end_line < start_line:
            raise AppError("Use positive line numbers with end_line >= start_line.")
        raw = self.content(path)
        if raw is None:
            self.input(path, None)
            self.save()
            return {"path": path, "status": "absent", "hash": "absent"}
        hashed = digest(raw)
        lines = raw.decode().splitlines(keepends=True)
        if start_line > max(len(lines), 1):
            raise AppError("start_line exceeds file length.")
        # Model-selected windows can span distant search hits. Return a bounded
        # page instead of forcing the model to retry the same invalid range.
        end = start_line - 1
        size = 0
        for line in lines[start_line - 1:min(end_line, start_line + 199, len(lines))]:
            size += len(line.encode())
            if size > self.settings.task_max_result_bytes:
                break
            end += 1
        if end < start_line and lines:
            raise AppError("Single line exceeds task_max_result_bytes; cannot return a complete line.")
        text = "".join(lines[start_line - 1:end])
        if self.manifest["read_bytes"] + len(text.encode()) > self.settings.task_max_read_bytes:
            raise AppError("task_max_read_bytes exhausted.")
        if path not in self.manifest["changes"]:
            self.input(path, raw)
        self.manifest["read_bytes"] += len(text.encode())
        source = {"path": path, "hash": hashed, "start_line": start_line, "end_line": end,
                  "text": text, "kind": "documentation" if path.endswith(".md") else "code"}
        identifier = next((k for k, v in self.manifest["sources"].items() if v == source), None)
        if identifier is None:
            identifier = "S" + str(len(self.manifest["sources"]) + 1)
            self.manifest["sources"][identifier] = source
        self.save()
        range_limited = end < min(end_line, len(lines))
        remaining_end = min(end_line, len(lines)) if range_limited else min(end + 200, len(lines))
        return {**source, "edit_hash": hashed[:16], "source_id": identifier, "status": "ok", "total_lines": len(lines),
                "truncated": end < len(lines), "range_limited": range_limited,
                "next_read": {"path": path, "start_line": end + 1, "end_line": remaining_end} if end < len(lines) else None,
                "note": "Only start_line..end_line above were read. If needed, call read_file with next_read or a focused range around unread matched_lines; do not repeat the same page.",
                "overlay": path in self.manifest["changes"]}

    def search_text(self, query, glob="**/*", limit=15):
        self.validate_glob(glob)
        if not query or len(query) > 200 or not 1 <= limit <= self.settings.task_max_search_results:
            raise AppError(f"Use a literal query of 1–200 characters and limit between 1 and {self.settings.task_max_search_results}.")
        paths, truncated = self.listing()
        hits, scanned, bytes_scanned, output_bytes = [], 0, 0, 0
        skipped = 0
        for path in paths:
            if not matches(path, glob):
                continue
            try:
                raw = self.content(path)
            except AppError as exc:
                if str(exc).startswith("conflict:"):
                    raise
                skipped += 1
                continue
            if raw is None:
                continue
            bytes_scanned += len(raw)
            if bytes_scanned > self.settings.task_max_search_bytes:
                truncated = True
                break
            scanned += 1
            lines = raw.decode().splitlines()
            numbers = [n for n, line in enumerate(lines, 1) if query.casefold() in line.casefold()]
            if not numbers:
                continue
            number = numbers[0]
            snippet = "\n".join(lines[max(0, number - 2):number + 1])
            hit = {"path": path, "line": number, "matched_lines": numbers[:30], "line_hits_truncated": len(numbers) > 30,
                   "text": snippet, "hash": digest(raw)}
            size = len(json.dumps(hit, ensure_ascii=False).encode())
            if size + output_bytes > self.settings.task_max_result_bytes:
                truncated = True
                break
            hits.append(hit)
            if path not in self.manifest["changes"]:
                self.input(path, raw)
            output_bytes += size
            if len(hits) == limit:
                truncated = True
                break
        self.save()
        return {"hits": hits, "scanned_files": scanned, "skipped_files": skipped, "truncated": truncated,
                "note": "One hit per file; matched_lines lists other occurrences to read. Glob **/*.ts excludes .tsx; use **/* or another search if needed. Not proof of all semantic uses."}

    def get_diff(self):
        base = self.manifest["comparison"]["base_sha"]
        paths, truncated = self.listing()
        if base:
            changed = git(self.settings, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base, "--").decode().split("\0")
            untracked = git(self.settings, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
            paths = sorted({p for p in changed + untracked if p})
        changes, budget = [], self.settings.task_max_result_bytes
        for path in paths:
            try:
                self.files.allowed(path)
                # Comparison is project state, never the proposed documentation overlay.
                current = self.files.read(path)
                old = git(self.settings, "show", f"{base}:{path}", optional=True) if base else None
                if old is not None:
                    if len(old) > self.settings.task_max_file_bytes or b"\0" in old:
                        continue
                    public_text(old.decode())
                if old == current:
                    continue
                self.input(path, current)
                patch = unified_patch((old or b"").decode(), (current or b"").decode(), "a/" + path if old is not None else "/dev/null", "b/" + path if current is not None else "/dev/null")
                size = len(patch.encode())
                changes.append({"path": path, "before_hash": digest(old) if old is not None else None,
                    "hash": digest(current) if current is not None else None, "patch": patch if size <= budget else "", "truncated": size > budget})
                budget -= min(size, budget)
                if len(changes) >= 50:
                    truncated = True
                    break
            except (AppError, UnicodeError) as exc:
                if str(exc).startswith("conflict:"):
                    raise
                truncated = True
        self.manifest["diff_seen"] = True
        # Persist discovery separately from conversation history. Diff is NOT a
        # read, and must not disappear when earlier tool messages are compacted.
        self.manifest["diff_changes"] = changes
        self.save()
        return {"comparison": self.manifest["comparison"], "changes": changes, "truncated": truncated or any(c["truncated"] for c in changes),
                "note": "Minus lines are historical base content, NOT current files. Plus lines and read_file are current. An already updated document may appear in this diff; do not apply the same update twice."}

    def pending_diff_reads(self):
        """Current hunk windows not yet covered by version-matched MCP reads."""
        pending = []
        for change in self.manifest.get("diff_changes", []):
            if change["hash"] is None:  # Deleted files have no current text.
                continue
            path = change["path"]
            version = self.manifest["changes"].get(path, {}).get("after_hash", change["hash"])
            reads = [s for s in self.manifest["sources"].values()
                     if s["path"] == path and s["hash"] == version]
            ranges = [(int(start), int(count or "1")) for start, count in
                      re.findall(r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", change["patch"])]
            if not ranges:  # Oversized diff: at least inspect the current file.
                if not reads:
                    pending.append({"path": path, "start_line": 1, "end_line": 100})
                continue
            for start, count in ranges:
                # Empty-file/deletion-only hunks need no nonexistent line read.
                uncovered = [n for n in range(max(1, start), start + count)
                             if not any(s["start_line"] <= n <= s["end_line"] for s in reads)]
                if uncovered:
                    pending.append({"path": path, "start_line": uncovered[0],
                                    "end_line": min(uncovered[-1], uncovered[0] + 99)})
        return pending

    def require_diff_reads(self):
        pending = self.pending_diff_reads()
        if pending:
            raise AppError("Read the current changed code/document hunk before proposing or finishing. "
                           "Use read_file: " + json.dumps(pending[:5]))

    def needs_related_code(self):
        if not any(c["hash"] and not c["path"].endswith(".md") for c in self.manifest.get("diff_changes", [])):
            return False
        code_reads = {s["path"] for s in self.manifest["sources"].values() if s["kind"] == "code"}
        available = sum(not p.endswith(".md") for p in self.listing()[0])
        return len(code_reads) < min(2, available)

    def search_context(self, query, glob="**/*", limit=5):
        self.validate_glob(glob)
        if not query or len(query) > 500 or not 1 <= limit <= 10:
            raise AppError("Invalid semantic query/limit.")
        from .ollama_client import Ollama
        paths, truncated = self.listing()
        eligible = [p for p in paths if matches(p, glob)]
        words = re.findall(r"\w+", query.casefold())
        eligible.sort(key=lambda p: (-sum(w in p.casefold() for w in words), p))
        selected = eligible[:self.settings.task_rag_files]
        sources, skipped = [], 0
        for path in selected:
            try:
                raw = self.content(path)
                if raw is None:
                    continue
                sources.append({"path": path, "text": raw.decode(), "kind": "documentation" if path.endswith(".md") else "code"})
            except AppError as exc:
                if str(exc).startswith("conflict:"):
                    raise
                skipped += 1
        if not sources:
            return {"hits": [], "truncated": truncated, "skipped": skipped}
        # Reuse production SQLite/NumPy index and hash/digest embedding cache.
        index = ReviewIndex(self.settings, "task/local", 0, self.run_id)
        index.root = self.directory / "rag"
        index.root.mkdir(exist_ok=True)
        index.cache = index.root / "embeddings.sqlite3"
        ollama = Ollama(self.settings)
        try:
            snapshot, stats = index.update_sources(ollama, sources)
            hits = search(snapshot, normalize(ollama.embed([query]), 1)[0], limit)
            results = []
            output_bytes = 0
            result_truncated = False
            for hit in hits:
                chunk = hit.chunk
                raw = self.content(chunk.path)
                if raw is None or digest(raw) != digest(next(s["text"].encode() for s in sources if s["path"] == chunk.path)):
                    raise AppError("conflict: RAG source changed during search.")
                actual = "".join(raw.decode().splitlines(keepends=True)[chunk.start_line - 1:chunk.end_line])
                if chunk.text not in actual:
                    raise AppError("RAG line/version mismatch.")
                item = {"path": chunk.path, "start_line": chunk.start_line, "end_line": chunk.end_line,
                    "text": chunk.text, "hash": digest(raw), "kind": chunk.source_type, "symbol": chunk.symbol, "score": hit.score}
                size = len(json.dumps(item, ensure_ascii=False).encode())
                if output_bytes + size > self.settings.task_max_result_bytes:
                    result_truncated = True
                    break
                results.append(item)
                if chunk.path not in self.manifest["changes"]:
                    self.input(chunk.path, raw)
                output_bytes += size
            self.manifest["rag"] = {**stats, "files": len(sources), "eligible_files": len(eligible), "chunks": len(snapshot.chunks)}
            self.save()
            return {"hits": results, "stats": self.manifest["rag"], "truncated": truncated or len(selected) < len(eligible) or bool(skipped) or result_truncated,
                    "note": "Verified against current overlay. Use read_file for source IDs and editing."}
        finally:
            ollama.close()

    def validate_markdown(self, path, text, source_ids):
        public_text(text)
        sources = self.manifest["sources"]
        if not source_ids or any(s not in sources for s in source_ids):
            raise AppError("Supply valid source IDs from read_file, not search/RAG.")
        for identifier in re.findall(r"\[(S\d+)\]", text):
            if identifier not in sources:
                raise AppError("Unknown citation: " + identifier)
        fence, sections = None, []
        for line in text.splitlines():
            match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
            if match:
                mark = match[1]
                if fence is None:
                    fence = mark
                elif mark[0] == fence[0] and len(mark) >= len(fence) and not match[2].strip():
                    fence = None
            elif fence is None:
                heading = re.match(r"^#{1,6}\s+(.+)$", line)
                if heading:
                    sections.append(heading[1])
        if fence:
            raise AppError("Unclosed Markdown code fence.")
        if len(sections) != len(set(sections)):
            raise AppError("Duplicate Markdown section headings.")
        # Validate new/changed links; leave unrelated existing document links alone.
        change = self.manifest["changes"].get(path)
        before = (self.blob("originals", path).read_bytes() if change["before_hash"] else None) if change else self.files.read(path)
        old_links = set(re.findall(r"\]\(([^)]+)\)", before.decode())) if before else set()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target in old_links:
                continue
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            resolved = posixpath.normpath(posixpath.join(str(PurePosixPath(path).parent), unquote(url.path))) if url.path else path
            raw = text.encode() if resolved == path else self.content(resolved)
            if raw is None:
                raise AppError("Broken relative link: " + resolved)
            if resolved != path and resolved not in self.manifest["changes"]:
                self.input(resolved, raw)
            if not resolved.endswith(".md") or url.fragment.startswith("L"):
                self.validate_reference(resolved, url.fragment)
            elif url.fragment:
                headings = re.findall(r"(?m)^#{1,6}\s+(.+?)(?:\s+#+)?$", raw.decode())
                slugs = {re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in headings}
                if unquote(url.fragment) not in slugs:
                    raise AppError("Unknown Markdown heading anchor: " + target)
        # Bare and inline-code references are checked too, excluding link targets
        # (resolved relative to this document above). Preserve unrelated old refs.
        pattern = r"(?<![\w./-])((?:[\w@-]+/)*[\w.-]+\.(?:tsx?|jsx?|py))(?::(\d+)(?:-(\d+))?)?(?![\w.])"
        def references(value):
            return set(re.findall(pattern, re.sub(r"\]\([^)]+\)", "]", value)))
        old_refs = references(before.decode()) if before else set()
        for ref, start, end in references(text) - old_refs:
            self.validate_reference(ref, "L" + start + ("-L" + end if end else "") if start else "")
        table_pattern = r"\|\s*`?([\w./-]+\.(?:tsx?|jsx?|py))`?\s*\|\s*`?(\d+)(?:-(\d+))?`?\s*\|"
        old_tables = set(re.findall(table_pattern, before.decode())) if before else set()
        for ref, start, end in set(re.findall(table_pattern, text)) - old_tables:
            self.validate_reference(ref, "L" + start + ("-L" + end if end else ""))

    def validate_reference(self, path, fragment):
        clean_path(path)
        seen = [s for s in self.manifest["sources"].values() if s["path"] == path]
        if not seen:
            raise AppError("Source path was not read: " + path)
        if fragment:
            match = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", fragment)
            if not match:
                raise AppError("Use source line anchors #L1-L3.")
            start, end = int(match[1]), int(match[2] or match[1])
            if end < start or not any(s["start_line"] <= start <= end <= s["end_line"] for s in seen):
                raise AppError("Source lines were not read: " + path)

    def propose_file_change(self, path, expected_hash, sources, content="", old_text="", new_text=""):
        if self.manifest["status"] != "running":
            raise AppError("Run is sealed.")
        self.files.allowed(path, write=True)
        self.require_diff_reads()
        if self.needs_related_code():
            raise AppError("Read related implementation, configuration definition or test before editing documentation. Do not infer configuration instructions from a property name in the diff.")
        current = self.content(path)
        if current is None:
            if expected_hash != "absent" or not content or old_text or new_text:
                raise AppError("Creation requires expected_hash='absent' and content only.")
            self.input(path, None)
            text = content
        else:
            hashed = digest(current)
            if path not in self.manifest["changes"]:
                self.input(path, current)
            reads = [s for s in self.manifest["sources"].values() if s["path"] == path and s["hash"] == hashed]
            if not reads:
                raise AppError("Read the current file with read_file before editing.")
            # A short selector is accepted only for an actually read FULL SHA-256.
            # All concurrency, provenance and apply checks still use full hashes.
            if expected_hash not in (hashed, hashed[:16]):
                raise AppError(f"Invalid expected_hash argument. Copy edit_hash='{hashed[:16]}' from read_file; never invent a hash.")
            if old_text and old_text not in current.decode() and new_text and new_text in current.decode():
                raise AppError("Requested new_text is ALREADY PRESENT in the current file; old_text is historical or stale. Verify current code/tests and finish_task(no_changes=true) if no other changes are needed. Do not repeat this replacement.")
            if content or not old_text or not any(old_text in s["text"] for s in reads):
                raise AppError("Editing requires read_file of EXACT old_text; copy punctuation, whitespace and Markdown bullet prefixes. Prefer one short unique sentence. Use old_text/new_text, not content.")
            text = current.decode()
            if text.count(old_text) != 1:
                raise AppError("old_text must match exactly once.")
            text = text.replace(old_text, new_text, 1)
        raw = text.encode()
        self.validate_markdown(path, text, sources)
        if len(raw) > self.settings.task_max_file_bytes or len(raw) + sum(c["size"] for p, c in self.manifest["changes"].items() if p != path) > self.settings.task_max_change_bytes:
            raise AppError("Change byte limit exceeded.")
        if path not in self.manifest["changes"] and len(self.manifest["changes"]) >= self.settings.task_max_changes:
            raise AppError("Changed file limit exceeded.")
        original = self.files.read(path, write=True)
        if path not in self.manifest["changes"]:
            self.input(path, original)
            if original is not None:
                target = self.blob("originals", path)
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(original)
        if original == raw:
            self.manifest["changes"].pop(path, None)
        else:
            target = self.blob("files", path)
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
            self.manifest["changes"][path] = {"before_hash": digest(original) if original is not None else None,
                "after_hash": digest(raw), "size": len(raw), "sources": sources}
        self.manifest["revision"] += 1
        self.save()
        return {"path": path, "status": "proposed" if original != raw else "unchanged", "hash": digest(raw), "written_to_project": False,
                "note": "No bytes changed; this is not a completed update." if original == raw else "Prepared in overlay; inspect with preview_changes."}

    def preview_changes(self):
        patches = []
        for path, change in self.manifest["changes"].items():
            original = self.blob("originals", path).read_text() if change["before_hash"] else ""
            after = self.blob("files", path).read_text()
            patches.append(unified_patch(original, after, "a/" + path if change["before_hash"] else "/dev/null", "b/" + path))
        patch = "\n".join(patches)
        (self.directory / "changes.patch").write_text(patch)
        self.manifest["preview_revision"] = self.manifest["revision"]
        self.save()
        limit = self.settings.task_max_result_bytes
        return {"changes": [{"path": p, **c} for p, c in self.manifest["changes"].items()],
                "diff": patch.encode()[:limit].decode("utf-8", "ignore"), "truncated": len(patch.encode()) > limit, "artifact": "changes.patch"}

    def finish_task(self, summary, no_changes=False):
        self.require_diff_reads()
        if self.needs_related_code():
            raise AppError("Read a related implementation/configuration/test file to verify the changed behavior before finishing.")
        if not summary or len(summary) > 2000:
            raise AppError("Supply a brief factual summary.")
        public_text(summary)
        if not self.manifest["sources"]:
            raise AppError("No files actually read.")
        if self.manifest["preview_revision"] != self.manifest["revision"]:
            raise AppError("Call preview_changes after the last proposal before finishing.")
        if bool(self.manifest["changes"]) == no_changes:
            raise AppError("No prepared files or inconsistent no_changes flag. To create a requested report, call propose_file_change with content, then preview_changes. A finish summary does not create a file; do not flip no_changes to bypass this error.")
        if no_changes:
            complete_doc = any(s["path"].endswith(".md") and s["start_line"] == 1 and s["end_line"] >= len((self.content(s["path"]) or b"").splitlines()) for s in self.manifest["sources"].values())
            if not complete_doc:
                raise AppError("Before no_changes, read a complete current Markdown document, not just its unrelated notes. Verify its actual behavior description against current implementation/tests.")
            if not any(s["kind"] == "code" for s in self.manifest["sources"].values()):
                raise AppError("Before no_changes, read actual implementation/tests to verify the document. Search/RAG alone cannot establish that documentation matches code.")
        minimum = min(3, len(self.listing()[0]))
        if len({s["path"] for s in self.manifest["sources"].values()}) < minimum:
            raise AppError(f"Read at least {minimum} DIFFERENT relevant files with read_file before finishing: implementation, related test/caller/type, and documentation. Search/RAG does not count as reading.")
        self.manifest.update(status="no_changes" if no_changes else "prepared", summary=summary, sealed=True)
        self.save()
        return {"status": self.manifest["status"], "run_id": self.run_id}
