from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from conftest import commit, git
from outline_assistant.config import AppError
from outline_assistant.task_apply import apply_run
from outline_assistant.task_files import SafeRoot, digest, write_json
from outline_assistant.task_workspace import Workspace, create_run


def setup(settings):
    root = settings.repo_path
    (root / "lib").mkdir()
    (root / "lib/action.ts").write_text("export function act() { return 7; }\n")
    (root / "README.md").write_text("# API\nReturns 6.\n\n## User section\nKeep my notes.\n")
    (root / "docs/extra.md").write_text("# Extra\nReturns 6.\n")
    commit(settings)
    w = Workspace(settings, create_run(settings, "Update docs"))
    source = w.read_file("lib/action.ts")
    return w, source["source_id"]


def test_changed_hunks_must_be_read_before_proposal_or_finish(settings):
    w, source = setup(settings)
    target = settings.repo_path / "lib/action.ts"
    target.write_text("\n".join(f"// line {i}" for i in range(1, 241)) + "\n")
    commit(settings)
    target.write_text(target.read_text().replace("// line 221\n", "export const result = 9;\n"))
    w = Workspace(settings, create_run(settings, "Update existing documentation"))
    diff = w.get_diff()
    assert diff["changes"][0]["path"] == "lib/action.ts"
    # Reading a related file, or the wrong part of the right file, is insufficient.
    doc = w.read_file("README.md")
    w.read_file("lib/action.ts", 1, 100)
    fresh = Workspace(settings, w.run_id)
    assert fresh.pending_diff_reads() == [{"path": "lib/action.ts", "start_line": 218, "end_line": 224}]
    with pytest.raises(AppError, match="current changed"):
        fresh.propose_file_change("README.md", doc["edit_hash"], [doc["source_id"]], old_text="Returns 6.", new_text="Returns 9.")
    fresh.preview_changes()
    with pytest.raises(AppError, match="current changed"):
        fresh.finish_task("Documentation is up to date", no_changes=True)
    fresh.read_file("lib/action.ts", 218, 220)
    assert fresh.pending_diff_reads()[0]["start_line"] == 221
    read = fresh.read_file("lib/action.ts", 221, 224)
    assert fresh.pending_diff_reads() == []
    fresh.propose_file_change("README.md", doc["edit_hash"], [read["source_id"], doc["source_id"]], old_text="Returns 6.", new_text="Returns 9.")
    assert target.read_text().count("result = 9") == 1
    assert "Returns 6." in (settings.repo_path / "README.md").read_text()


def test_forbidden_code_edit_points_to_document_discovery(settings):
    w, source = setup(settings)
    with pytest.raises(AppError, match="Source code and code comments are read-only"):
        w.propose_file_change("lib/action.ts", "wrong", [source], old_text="7", new_text="9")


def edit(w, path, source):
    read = w.read_file(path)
    return w.propose_file_change(path, read["hash"], [source, read["source_id"]], old_text="Returns 6.", new_text="Returns 7.")


def seal(w):
    for path in ("README.md", "docs/extra.md"):
        if len({s["path"] for s in w.manifest["sources"].values()}) < 3:
            w.read_file(path)
    w.preview_changes()
    w.finish_task("Updated from source evidence.")


@pytest.mark.parametrize("byte_limit", [12000, 80])
def test_long_reads_page_by_lines_and_bytes_without_claiming_unread_evidence(settings, byte_limit):
    config = replace(settings, task_max_result_bytes=byte_limit)
    content = "".join(f"line {n}\n" for n in range(1, 281))
    (config.repo_path / "README.md").write_text(content)
    w = Workspace(config, create_run(config, "Read a long document"))
    args = {"path": "README.md", "start_line": 5, "end_line": 233}
    pages = []
    while True:
        result = w.read_file(**args)
        pages.append(result["text"])
        assert result["end_line"] - result["start_line"] < 200
        assert len(result["text"].encode()) <= byte_limit
        recorded = w.manifest["sources"][result["source_id"]]
        assert recorded["end_line"] == result["end_line"]
        if len(pages) == 1:
            assert result["range_limited"]
            with pytest.raises(AppError, match="EXACT old_text"):
                w.propose_file_change("README.md", result["hash"], [result["source_id"]],
                                      old_text="line 233\n", new_text="Changed\n")
        if not result["range_limited"]:
            break
        args = result["next_read"]
        assert args["start_line"] == result["end_line"] + 1
    assert "".join(pages) == "".join(content.splitlines(keepends=True)[4:233])
    assert result["end_line"] == 233 and result["truncated"]
    assert result["next_read"]["start_line"] == 234
    last = w.read_file(**result["next_read"])
    assert last["start_line"] == 234


def test_read_page_invalid_ranges_single_line_and_eof(settings):
    config = replace(settings, task_max_result_bytes=40)
    (config.repo_path / "README.md").write_text("x" * 41 + "\n")
    w = Workspace(config, create_run(config, "Read"))
    with pytest.raises(AppError, match="Single line"):
        w.read_file("README.md", 1, 999)
    for start, end in [(0, 10), (10, 9)]:
        with pytest.raises(AppError, match="positive line numbers"):
            w.read_file("README.md", start, end)
    assert not w.manifest["sources"]
    (config.repo_path / "README.md").write_text("# Short\n")
    result = w.read_file("README.md", 1, 999)
    assert result["end_line"] == 1 and result["next_read"] is None
    assert not result["range_limited"] and not result["truncated"]
    (config.repo_path / "docs/empty.md").write_text("")
    assert w.read_file("docs/empty.md")["next_read"] is None


@pytest.mark.parametrize("tool", ["list_files", "search_text", "search_context"])
def test_unsupported_brace_glob_is_error_not_false_absence(settings, tool):
    w, _ = setup(settings)
    kwargs = {"glob": "**/*.{ts,tsx}"}
    if tool != "list_files":
        kwargs["query"] = "act"
    with pytest.raises(AppError, match="Use glob='"):
        getattr(w, tool)(**kwargs)
    assert w.search_text("act", glob="**/*")["hits"]


def test_overlay_consistency_preview_apply_replay_and_dirty_target(settings):
    w, source = setup(settings)
    # User changes predating the run/read must be preserved.
    original = (settings.repo_path / "README.md").read_text() + "\nUser uncommitted addition.\n"
    (settings.repo_path / "README.md").write_text(original)
    edit(w, "README.md", source)
    w.propose_file_change("reports/action.md", "absent", [source], content="# Action\nUses `lib/action.ts:1` [S1].\n")
    assert (settings.repo_path / "README.md").read_text() == original
    assert "Returns 7." in w.read_file("README.md")["text"]
    assert "reports/action.md" in w.list_files()["files"]
    assert any(h["path"] == "reports/action.md" for h in w.search_text("Uses")["hits"])
    seal(w)
    patch_text = (w.directory / "changes.patch").read_text()
    assert "+Returns 7." in patch_text and "+User uncommitted" not in patch_text
    settings.index_dir.mkdir()
    (settings.index_dir / "CURRENT").write_text("old-generation")
    assert apply_run(settings, w.run_id)["status"] == "applied"
    assert not (settings.index_dir / "CURRENT").exists()
    assert "User uncommitted addition" in (settings.repo_path / "README.md").read_text()
    assert apply_run(settings, w.run_id)["no_op"]
    (settings.repo_path / "reports/action.md").write_text("New user content")
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert (settings.repo_path / "reports/action.md").read_text() == "New user content"


@pytest.mark.parametrize("conflict", ["source", "target", "new_file", "head"])
def test_preflight_conflicts_never_partially_write(settings, conflict):
    w, source = setup(settings)
    edit(w, "README.md", source)
    w.propose_file_change("reports/new.md", "absent", [source], content="# New\nEvidence [S1].\n")
    seal(w)
    if conflict == "source":
        (settings.repo_path / "lib/action.ts").write_text("export const changed = true;\n")
    elif conflict == "target":
        (settings.repo_path / "README.md").write_text("User edit")
    elif conflict == "head":
        (settings.repo_path / "docs/extra.md").write_text("Different commit")
        commit(settings)
    else:
        (settings.repo_path / "reports").mkdir()
        (settings.repo_path / "reports/new.md").write_text("User new file")
    before = {str(p): p.read_bytes() for p in settings.repo_path.rglob("*.md")}
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert {str(p): p.read_bytes() for p in settings.repo_path.rglob("*.md")} == before


@pytest.mark.parametrize("path", ["../escape.md", "/tmp/escape.md", "docs/../README.md", "docs/.secret.md", "lib/action.ts", "other.md"])
def test_write_allowlist_and_paths(settings, path):
    w, source = setup(settings)
    with pytest.raises(AppError):
        w.propose_file_change(path, "absent", [source], content="# Bad\n")


def test_symlinks_unread_and_ambiguous_edits(settings, tmp_path):
    w, source = setup(settings)
    (settings.repo_path / "reports").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AppError):
        w.propose_file_change("reports/out.md", "absent", [source], content="# Out")
    (settings.repo_path / "docs/link.md").symlink_to(settings.repo_path / "README.md")
    with pytest.raises(AppError):
        w.read_file("docs/link.md")
    original = (settings.repo_path / "README.md").read_bytes()
    with pytest.raises(AppError, match="read_file"):
        w.propose_file_change("README.md", digest(original), [source], old_text="Returns 6.", new_text="Changed.")
    (settings.repo_path / "docs/extra.md").write_text("same\nsame\n")
    read = w.read_file("docs/extra.md")
    with pytest.raises(AppError, match="exactly once"):
        w.propose_file_change("docs/extra.md", read["hash"], [source], old_text="same", new_text="new")


@pytest.mark.parametrize("text", ["# Report\n```ts\nopen", "# Duplicate\nx\n# Duplicate\ny", "# R\n[S999]", "# R\n[link](../missing.md)", "# R\n`lib/action.ts:999`", "# R\n`lib/fake.ts`", "# R\nlib/action.ts:999", "# R\nlib/fake.ts", "# R\n| lib/action.ts | 999 | Fake |", "# R\n[link](../README.md#missing)", "# R\n[link](#missing)"])
def test_markdown_and_sources_reject_fabrications(settings, text):
    w, source = setup(settings)
    with pytest.raises(AppError):
        w.propose_file_change("reports/test.md", "absent", [source], content=text)


def test_secret_content_excluded_from_read_and_proposal(settings):
    w, source = setup(settings)
    (settings.repo_path / "docs/access.md").write_text("token=" + "x" * 40)
    with pytest.raises(AppError, match="Secret"):
        w.read_file("docs/access.md")
    with pytest.raises(AppError, match="Secret"):
        w.propose_file_change("reports/access.md", "absent", [source], content="token=" + "x" * 40)
    (settings.repo_path / "config.json").write_text(json.dumps({"password": "private-value-example"}))
    with pytest.raises(AppError, match="Secret"):
        w.read_file("config.json")


@pytest.mark.parametrize("user_edit", [False, True])
def test_multifile_rollback_preserves_foreign_changes(settings, user_edit):
    w, source = setup(settings)
    edit(w, "README.md", source)
    edit(w, "docs/extra.md", source)
    seal(w)
    actual_replace = SafeRoot.replace
    def fail_second(self, path, raw, expected):
        if path == "docs/extra.md":
            if user_edit:
                (settings.repo_path / "README.md").write_text("Concurrent user's edit")
            raise OSError("Injected disk error")
        return actual_replace(self, path, raw, expected)
    with patch.object(SafeRoot, "replace", fail_second):
        result = apply_run(settings, w.run_id)
    assert result["status"] == "conflict"
    assert "Returns 6." in (settings.repo_path / "docs/extra.md").read_text()
    text = (settings.repo_path / "README.md").read_text()
    assert text == "Concurrent user's edit" if user_edit else "Returns 6." in text
    journal = json.loads((w.directory / "apply-journal.json").read_text())
    assert journal["state"] == ("recovery_conflict" if user_edit else "rolled_back")


def test_restart_recovers_interrupted_journal(settings):
    w, source = setup(settings)
    edit(w, "README.md", source)
    seal(w)
    c = w.manifest["changes"]["README.md"]
    write_json(w.directory / "apply-journal.json", {"state": "writing", "intents": ["README.md"], "written": []})
    w.files.replace("README.md", w.blob("files", "README.md").read_bytes(), c["before_hash"])
    result = apply_run(settings, w.run_id)
    assert result["recovery"] == "rolled_back"
    assert "Returns 6." in (settings.repo_path / "README.md").read_text()
    assert apply_run(settings, w.run_id)["status"] == "applied"


def test_diff_default_includes_staged_unstaged_untracked_and_explicit_base(settings):
    w, source = setup(settings)
    base = git(settings, "rev-parse", "HEAD")
    (settings.repo_path / "lib/action.ts").write_text("export const staged = 1;\n")
    git(settings, "add", "lib/action.ts")
    (settings.repo_path / "README.md").write_text("# Unstaged\n")
    (settings.repo_path / "docs/new.md").write_text("# Untracked\n")
    w = Workspace(settings, create_run(settings, "Inspect existing local changes"))
    result = w.get_diff()
    assert {c["path"] for c in result["changes"]} == {"lib/action.ts", "README.md", "docs/new.md"}
    assert result["comparison"]["base_sha"] == base
    commit(settings)
    other = Workspace(settings, create_run(settings, "Docs", base))
    assert len(other.get_diff()["changes"]) == 3


def test_rag_rechecks_overlay_and_reuses_embedding_cache(settings, fake):
    w, source = setup(settings)
    with patch("outline_assistant.ollama_client.Ollama", return_value=fake):
        fake.close = lambda: None
        first = w.search_context("Returns", glob="README.md")
        assert first["hits"] and first["stats"]["embedded"] > 0
        edit(w, "README.md", source)
        second = w.search_context("Returns", glob="README.md")
        expected = w.read_file("README.md")["hash"]
        assert all(h["hash"] == expected and "Returns 6" not in h["text"] for h in second["hits"])
        assert w.search_context("Returns", glob="README.md")["stats"]["embedded"] == 0
    assert "Returns 6" in (settings.repo_path / "README.md").read_text()


def test_preview_and_seal_required_and_tampered_output(settings):
    w, source = setup(settings)
    edit(w, "README.md", source)
    with pytest.raises(AppError, match="preview"):
        w.finish_task("Done")
    seal(w)
    w.blob("files", "README.md").write_text("Tampered")
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert "Returns 6" in (settings.repo_path / "README.md").read_text()


def test_short_hash_requires_full_read_version_and_corrects_bad_argument(settings):
    w, source = setup(settings)
    read = w.read_file("README.md")
    with pytest.raises(AppError, match="Invalid expected_hash"):
        w.propose_file_change("README.md", "0" * 16, [source], old_text="Returns 6.", new_text="Returns 7.")
    w.propose_file_change("README.md", read["edit_hash"], [source], old_text="Returns 6.", new_text="Returns 7.")
    assert w.manifest["inputs"]["README.md"] == read["hash"]
    seal(w)
    (settings.repo_path / "lib/action.ts").write_text("Concurrent source change")
    assert apply_run(settings, w.run_id)["status"] == "conflict"


def test_unified_diff_missing_newline_is_valid_git_patch(settings):
    w, source = setup(settings)
    (settings.repo_path / "docs/extra.md").write_text("# Extra\nReturns 6.")
    edit(w, "docs/extra.md", source)
    w.propose_file_change("reports/new.md", "absent", [source], content="# New\nEvidence [S1].")
    seal(w)
    assert "\\ No newline at end of file" in (w.directory / "changes.patch").read_text()
    git(settings, "apply", "--check", str(w.directory / "changes.patch"))


def test_apply_run_cli_does_not_construct_model(settings, monkeypatch, capsys):
    from outline_assistant.cli import main
    w, source = setup(settings)
    edit(w, "README.md", source)
    seal(w)
    monkeypatch.setattr("sys.argv", ["outline-assistant", "apply-run", w.run_id])
    monkeypatch.setattr("outline_assistant.cli.load_settings", lambda _: settings)
    with patch("outline_assistant.cli.Ollama", side_effect=AssertionError("Must not initialize LLM")):
        for _ in range(2):
            with pytest.raises(SystemExit) as stopped:
                main()
            assert stopped.value.code == 0
    assert '"no_op": true' in capsys.readouterr().out


def test_apply_rechecks_symlink_after_preparation(settings, tmp_path):
    w, source = setup(settings)
    edit(w, "docs/extra.md", source)
    seal(w)
    outside = tmp_path / "outside.md"
    outside.write_text("Keep outside")
    target = settings.repo_path / "docs/extra.md"
    target.unlink()
    target.symlink_to(outside)
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert outside.read_text() == "Keep outside"


def test_no_changes_requires_current_document_and_no_overlay(settings):
    w, source = setup(settings)
    p = settings.repo_path / "README.md"
    p.write_text(p.read_text().replace("Returns 6", "Returns 7"))
    w.preview_changes()
    with pytest.raises(AppError, match="complete current Markdown"):
        w.finish_task("Already current", no_changes=True)
    w.read_file("README.md")
    with pytest.raises(AppError, match="DIFFERENT relevant files"):
        w.finish_task("Examined existing documentation", no_changes=True)
    w.read_file("docs/extra.md")
    assert w.finish_task("Examined existing documentation", no_changes=True)["status"] == "no_changes"
    assert apply_run(settings, w.run_id)["no_op"]


def test_markdown_fenced_comments_and_links_are_checked_on_apply(settings):
    w, source = setup(settings)
    w.propose_file_change("reports/links.md", "absent", [source], content="# Links\n[API](../README.md#api)\n```python\n# comment\n# comment\n```\n")
    seal(w)
    (settings.repo_path / "README.md").write_text("# Renamed\n")
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert not (settings.repo_path / "reports/links.md").exists()


def test_replay_checks_sources_after_successful_application(settings):
    w, source = setup(settings)
    edit(w, "README.md", source)
    seal(w)
    assert apply_run(settings, w.run_id)["status"] == "applied"
    (settings.repo_path / "lib/action.ts").write_text("New behavior after application")
    assert apply_run(settings, w.run_id)["status"] == "conflict"
    assert "Returns 7" in (settings.repo_path / "README.md").read_text()


def test_literal_search_diversifies_files_and_has_independent_scan_budget(settings):
    w, _ = setup(settings)
    (settings.repo_path / "a.ts").write_text("needle();\n" * 100)
    (settings.repo_path / "b.ts").write_text("needle();\n")
    w.settings = replace(settings, task_max_read_bytes=1, task_max_search_bytes=20000)
    result = w.search_text("needle", limit=2)
    assert [h["path"] for h in result["hits"]] == ["a.ts", "b.ts"]
    assert result["hits"][0]["line_hits_truncated"] and len(result["hits"][0]["matched_lines"]) == 30
    w.settings = replace(w.settings, task_max_search_bytes=100)
    result = w.search_text("needle")
    assert result["truncated"] and not result["hits"]
