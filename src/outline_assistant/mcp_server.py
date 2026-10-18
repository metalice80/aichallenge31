import argparse
import logging
import os
import json
from pathlib import Path
import subprocess

from mcp.server.fastmcp import FastMCP

from .documents import safe_files


def create_server(root: Path, timeout: float = 10.0, review_settings=None):
    server = FastMCP("outline-read-only", log_level="WARNING")
    root = root.resolve()

    def git(*arguments, allow_failure=False):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *arguments], capture_output=True, text=True, timeout=timeout)
        if result.returncode and not allow_failure:
            raise ValueError(f"Git: {result.stderr.strip()}")
        return result.stdout.strip() if result.returncode == 0 else None

    @server.tool()
    def get_git_context() -> dict:
        """Read branch and commit; handle detached and unborn HEAD without mutations."""
        if not root.is_dir():
            raise ValueError("repo_path не существует")
        if git("rev-parse", "--is-inside-work-tree") != "true":
            raise ValueError("repo_path не является рабочим Git-репозиторием")
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("repo_path должен указывать на корень Git-репозитория")
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        commit = git("rev-parse", "--verify", "HEAD", allow_failure=True)
        return {"branch": branch, "commit": commit, "detached": branch is None and commit is not None, "unborn": commit is None}

    @server.tool()
    def list_project_files(limit: int = 80) -> dict:
        """Return a bounded listing of file names; this says nothing about their purpose."""
        if not 1 <= limit <= 500:
            raise ValueError("limit должен быть от 1 до 500")
        if not root.is_dir():
            raise ValueError("repo_path не существует")
        files = []
        for name in safe_files(root):
            if len(files) == limit:
                return {"files": files, "truncated": True}
            files.append(name)
        return {"files": files, "truncated": False}

    if review_settings is not None:
        from .review_mcp import register_review_tools
        register_review_tools(server, review_settings)

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--git-timeout", type=float, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    from .config import Settings
    data = os.environ.get("OUTLINE_REVIEW_SETTINGS")
    review_settings = Settings(**json.loads(data)) if data else None
    create_server(args.repo, args.git_timeout, review_settings).run(transport="stdio")


if __name__ == "__main__":
    main()
