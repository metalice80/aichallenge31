"""Controller-only application and recoverable multi-file journal; no LLM."""
import json

from .config import AppError
from .git_snapshot import file_lock
from .store import Index
from .task_files import digest, identity, write_json
from .task_workspace import Workspace


def recover(workspace, journal):
    """Restore only hashes written by us. Never roll back subsequent user edits."""
    errors = []
    for path in reversed(journal["intents"]):
        change = workspace.manifest["changes"][path]
        try:
            current = workspace.files.read(path, write=True)
            hashed = digest(current) if current is not None else None
            if hashed == change["before_hash"]:
                continue
            if hashed != change["after_hash"]:
                raise AppError("User-modified version preserved: " + path)
            if change["before_hash"] is None:
                workspace.files.undo_create(path, change["after_hash"])
            else:
                before = workspace.blob("originals", path).read_bytes()
                if digest(before) != change["before_hash"]:
                    raise AppError("Backup checksum mismatch: " + path)
                workspace.files.replace(path, before, change["after_hash"])
        except (AppError, OSError) as exc:
            errors.append(str(exc))
    journal.update(state="recovery_conflict" if errors else "rolled_back", errors=errors)
    write_json(workspace.directory / "apply-journal.json", journal)
    return errors


def invalidate_help(settings):
    # Existing generation mechanism: invalidate CURRENT under the same lock.
    # Next /help rebuilds from actual project contents, never from an overlay.
    index = Index(settings)
    with index.locked():
        (index.root / "CURRENT").unlink(missing_ok=True)


def apply_run(settings, run_id):
    with file_lock(settings.task_runs_dir / "apply.lock"):
        workspace = Workspace(settings, run_id)
        m = workspace.manifest
        journal_path = workspace.directory / "apply-journal.json"
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
        if journal and journal["state"] in ("writing", "recovery_conflict"):
            errors = recover(workspace, journal)
            m.update(status="conflict", application="recovered" if not errors else "manual_recovery_required")
            m["limitations"].append("Interrupted application recovered; " + ("; ".join(errors) if errors else "no project edits remain from that attempt."))
            workspace.save()
            return {"status": "conflict", "run_id": run_id, "recovery": journal["state"], "errors": errors}
        try:
            current_identity = identity(settings)
            if any(current_identity[k] != m["repository"][k] for k in ("root", "device", "inode")):
                raise AppError("conflict: repository identity changed.")
            if m["status"] == "applied" or (journal and journal["state"] == "committed"):
                if current_identity["head"] != m["repository"]["head"]:
                    raise AppError("conflict: HEAD changed after application; repeat analysis.")
                for path, expected in m["inputs"].items():
                    if path in m["changes"]:
                        expected = m["changes"][path]["after_hash"]
                    raw = workspace.files.read(path)
                    if (digest(raw) if raw is not None else None) != expected:
                        raise AppError("conflict: applied package source or output changed: " + path)
                for path, change in m["changes"].items():
                    raw = workspace.files.read(path, write=True)
                    if raw is None or digest(raw) != change["after_hash"]:
                        raise AppError("conflict: already applied output changed: " + path)
                if not m.get("index_invalidated"):
                    invalidate_help(settings)
                    m["index_invalidated"] = True
                m["status"] = "applied"
                workspace.save()
                return {"status": "applied", "run_id": run_id, "no_op": True}
            if m["status"] == "no_changes":
                return {"status": "no_changes", "run_id": run_id, "no_op": True}
            if m["status"] not in ("prepared", "conflict") or not m["changes"] or not m.get("sealed"):
                raise AppError("Only a validated prepared package can be applied.")
            if m["preview_revision"] != m["revision"]:
                raise AppError("Package has no final preview.")
            if current_identity["head"] != m["repository"]["head"]:
                raise AppError("conflict: HEAD changed; repeat analysis.")
            for path, expected in m["inputs"].items():
                raw = workspace.files.read(path)
                if (digest(raw) if raw is not None else None) != expected:
                    raise AppError("conflict: source or target changed: " + path)
            if len(m["changes"]) > settings.task_max_changes or sum(c["size"] for c in m["changes"].values()) > settings.task_max_change_bytes:
                raise AppError("Package exceeds current change limits.")
            for path, change in m["changes"].items():
                workspace.files.allowed(path, write=True)
                if path not in m["inputs"] or m["inputs"][path] != change["before_hash"]:
                    raise AppError("Missing target baseline.")
                raw = workspace.blob("files", path).read_bytes()
                if digest(raw) != change["after_hash"] or len(raw) != change["size"]:
                    raise AppError("Package checksum mismatch.")
                if change["before_hash"] and digest(workspace.blob("originals", path).read_bytes()) != change["before_hash"]:
                    raise AppError("Original checksum mismatch.")
                workspace.validate_markdown(path, raw.decode(), change["sources"])
            journal = {"state": "writing", "intents": [], "written": []}
            write_json(journal_path, journal)
            try:
                for path, change in m["changes"].items():
                    # Recheck evidence before each write. Already written targets use after hashes.
                    for source, expected in m["inputs"].items():
                        if source in journal["written"]:
                            expected = m["changes"][source]["after_hash"]
                        raw = workspace.files.read(source)
                        if (digest(raw) if raw is not None else None) != expected:
                            raise AppError("conflict during application: " + source)
                    journal["intents"].append(path)
                    write_json(journal_path, journal)
                    workspace.files.replace(path, workspace.blob("files", path).read_bytes(), change["before_hash"])
                    journal["written"].append(path)
                    write_json(journal_path, journal)
                journal["state"] = "committed"
                write_json(journal_path, journal)
            except BaseException:
                errors = recover(workspace, journal)
                m.update(status="conflict", application="manual_recovery_required" if errors else "rolled_back")
                m["limitations"].extend(errors)
                workspace.save()
                raise
            m["status"] = "applied"
            workspace.save()
            invalidate_help(settings)
            m["index_invalidated"] = True
            workspace.save()
            return {"status": "applied", "run_id": run_id, "no_op": False, "paths": list(m["changes"])}
        except (AppError, OSError, ValueError, KeyError) as exc:
            if str(exc).startswith("conflict:") or not journal or journal["state"] != "committed":
                m["status"] = "conflict"
            m["limitations"].append(str(exc))
            workspace.save()
            return {"status": m["status"], "run_id": run_id, "error": str(exc)}
