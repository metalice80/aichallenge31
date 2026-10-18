import asyncio
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from unittest.mock import patch

import pytest

from conftest import commit
from outline_assistant.config import AppError
from outline_assistant.mcp_client import connect
from outline_assistant.ollama_client import Ollama
from outline_assistant.task_apply import apply_run
from outline_assistant.task_mcp import TASK_TOOLS
from outline_assistant.task_runner import compact, run_task
from outline_assistant.task_workspace import Workspace, create_run


@contextmanager
def embedding_fixture():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def output(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            self.output({"models": [{"name": "bge-m3:latest", "digest": "fixture"}]})
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/embed" and data["truncate"] is False
            self.output({"embeddings": [[n + 1 for n in sha256(s.encode()).digest()[:4]] for s in data["input"]]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


@pytest.mark.parametrize("folder,symbol", [("feature", "invoke"), ("packages/widgets", "launchThing")])
@pytest.mark.parametrize("mode", ["report", "docs"])
def test_multistep_real_stdio_fake_models_varied_paths(settings, fake, folder, symbol, mode):
    root = settings.repo_path
    (root / folder).mkdir(parents=True)
    definition = folder + "/entry.ts"
    caller = folder + "/consumer.ts"
    test = folder + "/entry.test.ts"
    for path, text in {definition: f"export function {symbol}() {{ return 2; }}\n", caller: f"import {{ {symbol} }} from './entry';\n{symbol}();\n", test: f"expect({symbol}()).toBe(2);\n", "docs/guide.md": "# Guide\nReturns 1.\n\n## Notes\nUser content.\n"}.items():
        (root / path).write_text(text)
    commit(settings)
    (root / definition).write_text(f"export function {symbol}() {{ return 3; }}\n")
    step, reads = 0, {}
    def chat(messages, tools):
        nonlocal step
        names = {t["function"]["name"] for t in tools}
        if step == 4:
            assert names == {"read_file", "get_diff"}
            assert definition in messages[1]["content"]
        else:
            assert TASK_TOOLS - {"finish_task", "propose_file_change"} <= names <= TASK_TOOLS
        assert ("propose_file_change" in names) == (step >= 7)
        assert ("finish_task" in names) == (step >= 8)
        for message in messages:
            if message["role"] == "tool" and message.get("tool_name") == "read_file":
                data = json.loads(message["content"])
                reads[data["path"]] = data
        actions = [lambda: call("list_files"), lambda: call("search_text", query=symbol),
                   lambda: call("search_context", query=symbol, limit=3), lambda: call("get_diff"),
                   lambda: call("read_file", path=definition), lambda: call("read_file", path=caller),
                   lambda: call("read_file", path=test), lambda: call("read_file", path="docs/guide.md")]
        if step < len(actions):
            result = actions[step]()
        elif step == 8:
            ids = [r["source_id"] for r in reads.values()]
            result = call("propose_file_change", path="reports/usage.md", expected_hash="absent", sources=ids,
                          content=f"# Usage\n`{definition}:1` defines the API. [{ids[0]}]\n\n## Coverage\nLiteral uses and tests examined.\n") if mode == "report" else call("propose_file_change", path="docs/guide.md", expected_hash=reads["docs/guide.md"]["hash"], sources=ids, old_text="Returns 1.", new_text="Returns 3.")
        elif step == 9:
            result = call("preview_changes")
        else:
            result = call("finish_task", summary="Verified sources and prepared documentation.")
        step += 1
        return result
    fake.tool_chat = chat
    with embedding_fixture() as url:
        config = replace(settings, ollama_url=url, num_ctx=32768)
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
        apply_requested = folder == "feature"
        result = asyncio.run(run_task(config, fake, "Analyze the API and prepare documentation", apply=apply_requested))
        assert result["status"] == ("applied" if apply_requested else "prepared"), result["limitations"]
        if not apply_requested:
            assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts} == before
        events = [json.loads(line) for line in (config.task_runs_dir / result["run_id"] / "events.jsonl").read_text().splitlines()]
        assert len({e["arguments"]["path"] for e in events if e.get("name") == "read_file"}) == 4
        assert any(e.get("name") == "search_context" and e["result"]["stats"]["embedded"] > 0 for e in events)
        with patch.object(Ollama, "__init__", side_effect=AssertionError("No model on replay")):
            assert apply_run(config, result["run_id"])["status"] == "applied"
            assert apply_run(config, result["run_id"])["no_op"]
        assert (root / definition).read_bytes() == before[str(root / definition)]
        if mode == "docs":
            assert "Returns 3." in (root / "docs/guide.md").read_text()
            assert "User content." in (root / "docs/guide.md").read_text()


def test_task_tools_not_available_to_help_or_review(settings):
    async def check():
        for options in ({}, {"review": True}):
            async with connect(settings, **options) as mcp:
                names = {t.name for t in (await mcp.session.list_tools()).tools}
                assert not names & TASK_TOOLS
        run_id = create_run(settings, "Inspect files")
        async with connect(settings, task_run_id=run_id) as mcp:
            names = {t.name for t in (await mcp.session.list_tools()).tools}
            assert TASK_TOOLS <= names
            assert not {"apply_run", "publish_review", "get_pr_info", "shell", "delete_file"} & names
            with pytest.raises(AppError):
                await mcp.call("apply_run", {"run_id": run_id})
    asyncio.run(check())


def test_controller_recovers_from_skipped_diff_and_code_edit(settings, fake):
    root = settings.repo_path
    (root / "worker.py").write_text("enabled = False\n")
    (root / "flags.py").write_text("development = True\n")
    (root / "README.md").write_text("# Options\nDisabled by default.\n")
    commit(settings)
    (root / "worker.py").write_text("enabled = development\n")
    step, reads = 0, {}
    def chat(messages, tools):
        nonlocal step
        for m in messages:
            if m["role"] == "tool" and m.get("tool_name") == "read_file":
                data = json.loads(m["content"])
                reads[data["path"]] = data
        actions = [
            lambda: call("get_diff"),
            # Deliberately bypass the visible tool set; controller must reject.
            lambda: call("search_text", query="development"),
            lambda: call("read_file", path="worker.py"),
            lambda: call("read_file", path="flags.py"),
            lambda: call("read_file", path="README.md"),
            lambda: call("propose_file_change", path="flags.py", expected_hash=reads["flags.py"]["edit_hash"], sources=["S2"], old_text="True", new_text="False"),
            lambda: call("propose_file_change", path="README.md", expected_hash=reads["README.md"]["edit_hash"], sources=["S1", "S2", "S3"], old_text="Disabled by default.", new_text="Enabled in development."),
            lambda: call("preview_changes"),
            lambda: call("finish_task", summary="Updated current documentation from changed code."),
        ]
        result = actions[step]()
        step += 1
        return result
    fake.tool_chat = chat
    result = asyncio.run(run_task(settings, fake, "Update documentation from code changes"))
    assert result["status"] == "prepared", result["limitations"]
    assert set(result["changes"]) == {"README.md"}
    assert (root / "flags.py").read_text() == "development = True\n"
    events = [json.loads(line) for line in (settings.task_runs_dir / result["run_id"] / "events.jsonl").read_text().splitlines()]
    failures = [e["result"]["error"] for e in events if e["kind"] == "tool_error"]
    assert len(failures) == 2
    assert "pending_diff_reads" in failures[0] and "path" in failures[1]


def test_compaction_keeps_unread_diff_evidence(settings):
    (settings.repo_path / "runtime.py").write_text("enabled = False\n")
    commit(settings)
    (settings.repo_path / "runtime.py").write_text("enabled = True\n")
    w = Workspace(settings, create_run(settings, "Update docs from changed behavior"))
    w.get_diff()
    messages = [{"role": "system", "content": "Use tools"}, {"role": "user", "content": w.manifest["goal"]}]
    for _ in range(5):
        messages += [call("search_text", query="unrelated"), {"role": "tool", "content": "x" * 4000}]
    compact(messages, [], replace(settings, num_ctx=7000), w)
    state = json.loads(messages[1]["content"].split("Current evidence references: ", 1)[1])
    assert state["pending_diff_reads"] == [{"path": "runtime.py", "start_line": 1, "end_line": 1}]
    assert state["changed_paths"] == ["runtime.py"]
    assert "-enabled = False" in state["diff_excerpt"]
    assert "+enabled = True" in state["diff_excerpt"]


def test_model_follows_large_read_continuation_over_real_mcp(settings, fake):
    root = settings.repo_path
    (root / "view.tsx").write_text("".join("inspectWidget();\n" if n in (232, 233) else f"// line {n}\n" for n in range(1, 281)))
    (root / "api.ts").write_text("export function inspectWidget() {}\n")
    (root / "README.md").write_text("# API\nExisting user documentation.\n")
    commit(settings)
    step, reads = 0, {}
    def chat(messages, tools):
        nonlocal step
        for message in messages:
            if message["role"] == "tool" and message.get("tool_name") == "read_file":
                data = json.loads(message["content"])
                assert "error" not in data
                reads[data["source_id"]] = data
        actions = [
            lambda: call("search_text", query="inspectWidget"),
            lambda: call("read_file", path="view.tsx", start_line=5, end_line=233),
            lambda: call("read_file", **reads["S1"]["next_read"]),
            lambda: call("read_file", path="api.ts"),
            lambda: call("read_file", path="README.md"),
            lambda: call("propose_file_change", path="README.md", expected_hash=reads["S4"]["edit_hash"],
                         sources=["api.ts"], old_text="Existing user documentation.", new_text="Wrong source ID."),
            lambda: call("propose_file_change", path="README.md", expected_hash=reads["S4"]["edit_hash"],
                         sources=list(reads), old_text="Existing user documentation.",
                         new_text="Existing user documentation.\n\nCalls: `view.tsx:232-233`. [S2]"),
            lambda: call("preview_changes"),
            lambda: call("finish_task", summary="Read the actual calls and updated the existing document."),
        ]
        result = actions[step]()
        step += 1
        return result
    fake.tool_chat = chat
    result = asyncio.run(run_task(settings, fake, "Find API uses and extend existing README; no new files."))
    assert result["status"] == "prepared", result["limitations"]
    assert reads["S1"]["end_line"] == 204
    assert reads["S2"]["start_line"] == 205 and reads["S2"]["end_line"] == 233
    assert "inspectWidget();" in reads["S2"]["text"]
    assert set(result["changes"]) == {"README.md"}
    assert (root / "README.md").read_text() == "# API\nExisting user documentation.\n"
    events = [json.loads(line) for line in (settings.task_runs_dir / result["run_id"] / "events.jsonl").read_text().splitlines()]
    failures = [e for e in events if e["kind"] == "tool_error"]
    assert len(failures) == 1 and "sources.0" in failures[0]["result"]["error"]


def test_caller_error_inside_mcp_is_not_mislabeled_as_transport_failure(settings):
    async def check():
        with pytest.raises(AppError) as caught:
            async with connect(settings):
                raise AppError("Repeated identical tool call without progress.")
        assert str(caught.value) == "Repeated identical tool call without progress."
    asyncio.run(check())


def test_compaction_budgets_evidence_summary_before_evicting_exchanges(settings):
    (settings.repo_path / "README.md").write_text("# Existing doc\n")
    w = Workspace(settings, create_run(settings, "Read and update the existing document"))
    w.read_file("README.md")
    messages = [{"role": "system", "content": "Use tools"}, {"role": "user", "content": w.manifest["goal"]}]
    for _ in range(3):
        messages += [call("read_file", path="README.md"),
                     {"role": "tool", "tool_name": "read_file", "content": "x" * 4000}]
    # Just enough for two exchanges BEFORE including the evidence summary.
    budget = int(len(json.dumps({"messages": messages[:2] + messages[4:], "tools": []}, ensure_ascii=False).encode()) / 2) + 10
    config = replace(settings, num_ctx=budget + settings.answer_tokens + 1200)
    compact(messages, [], config, w)
    assert len(json.dumps({"messages": messages, "tools": []}, ensure_ascii=False).encode()) / 2 <= budget
    assert w.manifest["goal"] in messages[1]["content"]
    assert '"id": "S1"' in messages[1]["content"]
    assert [m["role"] for m in messages[2:]] == ["assistant", "tool"]


@pytest.mark.parametrize("kind", ["loop", "invalid", "promise", "steps", "timeout", "context"])
def test_loop_limits_and_errors_are_not_success(settings, fake, kind):
    (settings.repo_path / "README.md").write_text("# Docs\n")
    config = replace(settings, task_max_steps=5, task_max_errors=2)
    if kind == "invalid":
        fake.tool_chat = lambda *_: call("read_file", path="README.md", start_line="bad")
    elif kind == "promise":
        fake.tool_chat = lambda *_: {"role": "assistant", "content": "I will change it", "tool_calls": []}
    elif kind == "timeout":
        config = replace(config, task_timeout=0.15)
        fake.tool_chat = lambda *_: (time.sleep(0.3), call("list_files"))[1]
    elif kind == "context":
        config = replace(config, num_ctx=6000)
        fake.tool_chat = lambda *_: call("list_files")
    elif kind == "steps":
        config = replace(config, task_max_steps=1)
        fake.tool_chat = lambda *_: call("read_file", path="README.md")
    else:
        fake.tool_chat = lambda *_: call("list_files")
    result = asyncio.run(run_task(config, fake, "Prepare a report"))
    assert result["status"] in ("failed", "partial"), result
    assert result["limitations"] and not (settings.repo_path / "reports").exists()


def test_native_ollama_tools_payload_and_thinking_not_saved(settings):
    import httpx
    received = []
    def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {**call("list_files"), "thinking": "private thoughts"}, "done_reason": "stop"})
    client = Ollama(settings)
    client.client.close()
    client.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle))
    result = client.tool_chat([{"role": "user", "content": "Goal"}], [{"type": "function", "function": {"name": "list_files"}}])
    assert result["tool_calls"] and "thinking" not in result
    assert received[0]["tools"] and received[0]["think"] is False and "format" not in received[0]
    client.close()


def test_report_io_error_does_not_relabel_applied_files(settings, fake):
    for path, text in {"a.py": "def api(): return 2\n", "b.py": "assert api() == 2\n", "README.md": "# API\nReturns 1.\n"}.items():
        (settings.repo_path / path).write_text(text)
    w = Workspace(settings, create_run(settings, "Update API docs"))
    reads = [w.read_file(p) for p in ("a.py", "b.py", "README.md")]
    w.propose_file_change("README.md", reads[-1]["hash"], [r["source_id"] for r in reads], old_text="Returns 1.", new_text="Returns 2.")
    w.preview_changes()
    fake.tool_chat = lambda *_: call("finish_task", summary="Updated from code and test.")
    with patch("outline_assistant.task_runner.create_run", return_value=w.run_id), patch("outline_assistant.task_runner.render_report", side_effect=OSError("Report storage unavailable")):
        with pytest.raises(OSError):
            asyncio.run(run_task(settings, fake, "Update API docs", apply=True))
    assert Workspace(settings, w.run_id).manifest["status"] == "applied"
    assert "Returns 2" in (settings.repo_path / "README.md").read_text()
