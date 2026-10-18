"""Review tools registered on the existing MCP server; no publication tool."""
from .config import AppError
from .github import GitHub
from .git_snapshot import GitSnapshot, source_kind


def register_review_tools(server, settings):
    github = GitHub(settings)
    snapshots = GitSnapshot(settings)
    contexts = {}

    def context(repo, number):
        github.check_repo(repo)
        if number not in contexts:
            raise AppError("Сначала вызовите get_pr_info для фиксации SHA.")
        return contexts[number]

    @server.tool()
    def get_review_health() -> dict:
        """Read repository access and runner visibility, without registering anything."""
        repo = settings.github_repo
        data = github.get(f"/repos/{repo}")
        result = {"repo": data["full_name"], "default_branch": data["default_branch"], "permissions": data.get("permissions"), "authenticated": github.authenticated}
        try:
            runners = github.get(f"/repos/{repo}/actions/runners")
            result["runners"] = [{"name": r["name"], "status": r["status"], "labels": [label["name"] for label in r["labels"]]} for r in runners["runners"]]
        except AppError as exc:
            result["runners_unverified"] = str(exc)
        return result

    @server.tool()
    def get_pr_info(repo: str, number: int, expected_head: str = "") -> dict:
        """Pin API base/head and calculate merge base in an isolated bare Git cache."""
        info = github.pr(repo, number)
        if expected_head and info["head_sha"] != expected_head:
            raise AppError("stale: head SHA PR отличается от ожидаемого события.")
        if info["base_repo"] != repo or info["head_repo"] != repo:
            raise AppError("MVP поддерживает только PR из веток целевого репозитория.")
        contexts[number] = snapshots.prepare(info)
        return contexts[number]

    @server.tool()
    def get_pr_files(repo: str, number: int) -> dict:
        """Paginated API metadata plus authoritative immutable Git file statuses."""
        info = context(repo, number)
        data = github.files(repo, number)
        data["git_files"] = snapshots.files(info["merge_base_sha"], info["head_sha"])
        data["incomplete"] |= len(data["files"]) != info["changed_files"]
        data["head_sha"] = info["head_sha"]
        return data

    @server.tool()
    def get_pr_diff(repo: str, number: int) -> dict:
        """Full local merge-base -> head diff with line mappings and explicit limits."""
        return snapshots.diff(context(repo, number))

    @server.tool()
    def read_file_at_ref(repo: str, ref: str, path: str) -> dict:
        """Read an allowed regular text blob at an already pinned SHA, without checkout."""
        github.check_repo(repo)
        return snapshots.read(ref, path)

    @server.tool()
    def list_snapshot_files(repo: str, ref: str) -> dict:
        """Bounded allowlisted source paths for the snapshot indexer."""
        github.check_repo(repo)
        paths = sorted(p for p in snapshots.tree(ref) if source_kind(p, settings))
        return {"files": paths[:settings.review_max_tree_files], "truncated": len(paths) > settings.review_max_tree_files, "total": len(paths)}

    @server.tool()
    def search_snapshot(repo: str, ref: str, terms: list[str], limit: int = 12) -> dict:
        """Bounded literal symbol search in the same immutable snapshot; never executes code."""
        github.check_repo(repo)
        if not 1 <= limit <= 30 or not 1 <= len(terms) <= 8 or any(not t or len(t) > 100 for t in terms):
            raise AppError("Допустимо 1–8 строк поиска длиной до 100, limit 1–30.")
        paths = sorted(p for p in snapshots.tree(ref) if source_kind(p, settings))
        hits = []
        scanned = 0
        region_limited = False
        for path in paths[:settings.review_max_search_files]:
            data = snapshots.read(ref, path)
            scanned += 1
            if data["status"] != "ok":
                continue
            lines = data["text"].splitlines(keepends=True)
            matches = [i for i, line in enumerate(lines) if any(term in line for term in terms)]
            # An import alone does not show the caller's contract. Prefer usages,
            # retaining several separate regions instead of stopping at the import.
            matches.sort(key=lambda i: (lines[i].lstrip().startswith("import "), i))
            regions = []
            for i in matches:
                if any(start <= i < end for start, end in regions):
                    continue
                start, end = max(0, i - 4), min(len(lines), i + 5)
                regions.append((start, end))
                hits.append({"path": path, "sha": ref, "start_line": start + 1, "end_line": end, "text": "".join(lines[start:end]), "kind": data["kind"]})
                if len(hits) == limit:
                    return {"hits": hits, "truncated": True, "scanned_files": scanned}
                if len(regions) == 3:
                    region_limited |= any(not any(a <= j < b for a, b in regions) for j in matches)
                    break
        return {"hits": hits, "truncated": region_limited or len(paths) > scanned, "scanned_files": scanned}
