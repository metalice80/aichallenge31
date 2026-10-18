"""Real Ollama + MCP + Git, with explicitly simulated PR metadata API.
No GitHub writes. All code changes happen in a temporary repository.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from outline_assistant.config import load_settings
from outline_assistant.ollama_client import Ollama
from outline_assistant.review import run_review

OUTPUT = Path(__file__).parent.resolve()
FILES = ["README.md", "docs/ARCHITECTURE.md", "docs/SERVICES.md", "docs/SECURITY.md", "docs/TRANSLATION.md", "docs/CODE_OF_CONDUCT.md", "shared/utils/shallowEqual.ts", "shared/utils/collections.ts", "shared/utils/naturalSort.ts", "shared/utils/naturalSort.test.ts", "app/stores/GroupsStore.ts"]


def git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "-c", "user.name=Local evaluation", "-c", "user.email=eval@example.invalid", "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


@contextmanager
def fixture_api(infos):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            parts = self.path.split("?")[0].split("/")
            try:
                number = int(parts[5])
                info = infos[number]
                if parts[-1] == "files":
                    data = [{"filename": info["changed_path"], "status": "modified", "additions": 1, "deletions": 1}]
                else:
                    data = {k: v for k, v in info.items() if k != "changed_path"}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            except (KeyError, IndexError, ValueError):
                self.send_response(404)
                self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def main():
    settings = load_settings()
    originals = {name: (settings.repo_path / name).read_bytes() for name in FILES}
    before = {"head": git(settings.repo_path, "rev-parse", "HEAD"), "status": git(settings.repo_path, "status", "--porcelain=v1"), "branch": git(settings.repo_path, "symbolic-ref", "--short", "HEAD"), "hashes": {name: sha256(raw).hexdigest() for name, raw in originals.items()}}
    results = {"started": datetime.now(timezone.utc).isoformat(), "real_ollama": True, "real_mcp_stdio": True, "real_git_snapshots": True, "github_api": "LOCAL FIXTURE ONLY; no real PR exists", "publication": "DISABLED; no GitHub mutations", "outline_before": before, "scenarios": []}
    with tempfile.TemporaryDirectory(prefix="outline-review-eval-") as temp:
        directory = Path(temp)
        root = directory / "repo"
        root.mkdir()
        git(root, "init", "-b", "main")
        for name, raw in originals.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        base = commit(root, "Copy actual Outline files for local evaluation")
        scenarios = [
            (101, "obvious_bug", "shared/utils/shallowEqual.ts", "if (a.length !== b.length) {\n    return false;", "if (a.length !== b.length) {\n    return true;", "Arrays of different lengths incorrectly compare equal; report bug at changed return."),
            (102, "breaking_contract", "shared/utils/naturalSort.ts", "  key: string | ((item: T) => string),", "  key: (item: T) => string,", "Existing string-key callers in collections.ts, GroupsStore.ts and naturalSort.test.ts become TypeScript-incompatible."),
            (103, "safe_refactor", "shared/utils/shallowEqual.ts", "  if (a === b) {", "  const sameReference = a === b;\n  if (sameReference) {", "Equivalent reference comparison; no bug/architecture finding expected."),
            (104, "docs_only", "docs/SERVICES.md", "it can be ran on", "it can be run on", "Grammar correction only; no bug/architecture finding expected."),
        ]
        infos = {}
        for number, name, path, old, new, expectation in scenarios:
            git(root, "switch", "-c", name, base)
            text = (root / path).read_text()
            assert old in text, f"Fixture no longer matches actual file {path}"
            (root / path).write_text(text.replace(old, new, 1))
            head = commit(root, name)
            infos[number] = {"number": number, "state": "open", "draft": False, "title": f"Update {path}", "body": "Local evaluation of an isolated source change.", "user": {"login": "metalice80"}, "changed_files": 1, "base": {"sha": base, "repo": {"full_name": settings.github_repo}}, "head": {"sha": head, "repo": {"full_name": settings.github_repo}}, "changed_path": path}
        with fixture_api(infos) as api_url:
            config = replace(settings, github_api_url=api_url, review_git_url=str(root), review_git_dir=directory / "git-cache", review_index_dir=directory / "rag-cache", review_max_index_files=30, review_max_search_files=100, publish_enabled=False, http_timeout=240, mcp_timeout=60)
            ollama = Ollama(config)
            try:
                for number, name, path, old, new, expectation in scenarios:
                    print(f"SCENARIO {name}: real Ollama, real MCP, fixture API", flush=True)
                    report = await run_review(config, ollama, settings.github_repo, number, OUTPUT / name, infos[number]["head"]["sha"], lambda message: print(message, flush=True))
                    found_bugs = [f for f in report["findings"] if f["category"] in ("bug", "architecture")]
                    # This is a coarse observed metric; semantic judgment is recorded separately.
                    outcome = bool(found_bugs) if name in ("obvious_bug", "breaking_contract") else not found_bugs
                    results["scenarios"].append({"name": name, "path": path, "expectation": expectation, "status": report["status"], "finding_count": len(report["findings"]), "observed_expected_presence": outcome, "rag": report.get("rag"), "limitations": report["limitations"], "report": name + "/review.json"})
                    (OUTPUT / "local-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            finally:
                ollama.close()
    after = {"head": git(settings.repo_path, "rev-parse", "HEAD"), "status": git(settings.repo_path, "status", "--porcelain=v1"), "branch": git(settings.repo_path, "symbolic-ref", "--short", "HEAD"), "hashes": {name: sha256((settings.repo_path / name).read_bytes()).hexdigest() for name in FILES}}
    results["outline_unchanged"] = before == after
    results["finished"] = datetime.now(timezone.utc).isoformat()
    (OUTPUT / "local-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results["scenarios"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
