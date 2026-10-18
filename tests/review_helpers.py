import json
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from conftest import commit, git


def setup_pr(settings):
    root = settings.repo_path
    (root / "shared/utils").mkdir(parents=True)
    (root / "README.md").write_text("# Test project\nFunctions must preserve existing callers.\n")
    (root / "shared/utils/math.ts").write_text("export function add(a: number, b: number) {\n  return a + b;\n}\n")
    (root / "shared/utils/math.test.ts").write_text("import { add } from './math';\nexpect(add(1, 2)).toBe(3);\n")
    commit(settings)
    merge = git(settings, "rev-parse", "HEAD")
    git(settings, "switch", "-c", "feature")
    (root / "shared/utils/math.ts").write_text("export function add(a: number, b: number) {\n  return a - b;\n}\n")
    commit(settings, "bug")
    head = git(settings, "rev-parse", "HEAD")
    git(settings, "switch", "main")
    (root / "README.md").write_text("# Test project\nUnrelated change on base.\n")
    commit(settings, "diverged base")
    base = git(settings, "rev-parse", "HEAD")
    info = {"number": 7, "state": "open", "title": "Change addition", "body": "Review the change", "draft": False, "user": {"login": "metalice80"}, "changed_files": 1, "base": {"sha": base, "repo": {"full_name": "metalice80/outline_t"}}, "head": {"sha": head, "repo": {"full_name": "metalice80/outline_t"}}}
    return info, merge


@contextmanager
def api_server(info):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if "/pulls/7/files" in self.path:
                payload = [{"filename": "shared/utils/math.ts", "status": "modified", "additions": 1, "deletions": 1}]
            elif self.path.endswith("/pulls/7"):
                payload = info
            elif self.path.endswith("/actions/runners"):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{}')
                return
            else:
                payload = {"full_name": "metalice80/outline_t", "default_branch": "main"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def review_settings(settings, tmp_path, api_url):
    return replace(settings, github_api_url=api_url, review_git_url=str(settings.repo_path), review_git_dir=tmp_path / "review-git", review_index_dir=tmp_path / "review-rag", github_retries=0, review_retries=1, review_max_index_files=20, num_ctx=20000)
