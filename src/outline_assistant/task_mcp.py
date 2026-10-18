"""Only registered for explicit task sessions. No apply/delete/shell tool."""
from .task_workspace import Workspace
from typing import Annotated
from pydantic import Field


TASK_TOOLS = {"list_files", "search_text", "read_file", "get_diff", "search_context", "propose_file_change", "preview_changes", "finish_task"}


def register_task_tools(server, settings, run_id):
    workspace = Workspace(settings, run_id)

    @server.tool()
    def list_files(glob: str = "**/*", offset: Annotated[int, Field(ge=0)] = 0, limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict:
        """List allowed project/overlay paths. One glob, no brace expansion; **/* includes all extensions. Paginate with next_offset; truncated means incomplete."""
        return workspace.list_files(glob, offset, limit)

    @server.tool()
    def search_text(query: Annotated[str, Field(min_length=1, max_length=200)], glob: str = "**/*", limit: Annotated[int, Field(ge=1, le=30)] = 15) -> dict:
        """Literal multi-file search including overlay. limit=1..30 FILES; one hit per file, matched_lines lists occurrences. Default glob **/* includes TS and TSX; brace expansion is unsupported. Returns hashes and coverage. Then read relevant ranges."""
        return workspace.search_text(query, glob, limit)

    @server.tool()
    def read_file(path: str, start_line: Annotated[int, Field(ge=1)] = 1, end_line: Annotated[int, Field(ge=1)] = 120) -> dict:
        """Read a bounded page (at most 200 lines and the byte limit). Larger ranges return a partial page with actual start/end, range_limited and next_read arguments. Follow next_read for unread lines; never repeat the same page. Returns source_id, full SHA256 and edit_hash for editing; absent paths return hash='absent'."""
        return workspace.read_file(path, start_line, end_line)

    @server.tool()
    def get_diff() -> dict:
        """Get configured base/HEAD vs staged+unstaged+untracked project changes (not proposed overlay). Includes exact comparison metadata, paths, hashes and bounded patches."""
        return workspace.get_diff()

    @server.tool()
    def search_context(query: Annotated[str, Field(min_length=1, max_length=500)], glob: str = "**/*", limit: Annotated[int, Field(ge=1, le=10)] = 5) -> dict:
        """Semantic BGE-M3 search through bounded code/docs corpus; versions verified against current overlay. Provides suggestions, not read_file source IDs. Optional single glob narrows scope; no brace expansion."""
        return workspace.search_context(query, glob, limit)

    @server.tool()
    def propose_file_change(
        path: Annotated[str, Field(pattern=r"\.md$", description="Markdown document only, inside configured write allowlist. NEVER source code or code comments.")],
        expected_hash: Annotated[str, Field(description="Existing file: copy edit_hash from read_file. New file only: absent.")],
        sources: list[Annotated[str, Field(pattern=r"^S[1-9][0-9]*$", description="read_file source_id, e.g. S1. Never a filename.")]],
        old_text: Annotated[str, Field(description="Existing file: exact unique snippet copied from read_file; required for editing, including adding a section.")] = "",
        new_text: Annotated[str, Field(description="Replacement for old_text only. To add a section, include the original snippet followed by the new section.")] = "",
        content: Annotated[str, Field(description="Only for a NEW absent file. Omit when editing an existing file; use old_text and new_text instead.")] = "",
    ) -> dict:
        """Prepare Markdown only, never writes project. Existing file: use old_text/new_text, NEVER content. To append, replace a unique read snippet with itself plus new text. Copy edit_hash and sources=['S1', 'S2'] from read_file. Only NEW files use expected_hash='absent' and content. Sources support the claims; cite only read paths/lines or [S1]."""
        return workspace.propose_file_change(path, expected_hash, sources, content, old_text, new_text)

    @server.tool()
    def preview_changes() -> dict:
        """Save/return current task-only diff and change hashes. Must call after final proposal before finish_task."""
        return workspace.preview_changes()

    @server.tool()
    def finish_task(summary: str, no_changes: bool = False) -> dict:
        """Finish after reading at least 3 DIFFERENT relevant files (if available) and final preview. Read code, related test/caller, documentation. no_changes=true only when verified documents are current or object absent. A summary does not create files."""
        return workspace.finish_task(summary, no_changes)
