"""Measure the real review pipeline; no model/API fixtures or publication.

Run from the project root: .venv/bin/python evaluation/day32/benchmark_index.py
Each invocation creates fresh, isolated 80/1000-file caches under .review-cache.
Only ReviewIndex timing is instrumented; selection, MCP, embeddings and chat use
the production implementation. Git preparation and Qwen analysis are reported
separately from index construction. Cold means empty embedding/snapshot cache,
not cold OS/Git/model caches. BGE is warmed before each measured review.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
from time import perf_counter

from outline_assistant.config import load_settings
from outline_assistant.mcp_client import connect
from outline_assistant.ollama_client import Ollama
from outline_assistant import review
from outline_assistant.review_index import ReviewIndex

OUTPUT = Path(__file__).resolve().parent / "index-benchmark"
RUNTIME_CONFIG = Path("/Users/sergey/ai/outline-review-runtime/config.toml")


def outline_state(path):
    def git(*args):
        return subprocess.run(["git", "--no-optional-locks", "-C", str(path), *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    return {"head": git("rev-parse", "HEAD"), "branch": git("symbolic-ref", "--short", "HEAD"),
            "status": git("status", "--porcelain=v1")}


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cache_parent = Path(".review-cache/index-benchmark").resolve()
    cache_parent.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="run-", dir=cache_parent))
    base = replace(load_settings(str(RUNTIME_CONFIG)), publish_enabled=False,
                   review_git_dir=cache_parent / "git", review_index_dir=cache / "initial")
    results = {"started": datetime.now(timezone.utc).isoformat(), "real_ollama": True,
               "real_mcp": True, "real_github_read_only": True, "publication": False,
               "cache_directory": str(cache), "outline_before": outline_state(base.repo_path),
               "settings": {key: getattr(base, key) for key in ("embedding_model", "chat_model",
                    "chunk_chars", "overlap_chars", "batch_size", "review_max_file_bytes",
                    "review_max_search_files", "review_max_tree_files")}, "runs": []}

    def save():
        (OUTPUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")

    try:
        async with connect(base, review=True) as mcp:
            info = await mcp.call("get_pr_info", {"repo": base.github_repo, "number": 1})
        results["pr"] = {key: info[key] for key in ("head_sha", "base_sha", "merge_base_sha", "state")}
        if info["state"] != "open":
            raise RuntimeError("Benchmark requires an open PR #1")
        save()
        for limit, mode in ((80, "cold"), (1000, "cold"), (80, "warm"), (1000, "warm")):
            config = replace(base, review_max_index_files=limit, review_index_dir=cache / str(limit))
            ollama = Ollama(config)
            row = {"limit": limit, "mode": mode}
            results["runs"].append(row)
            print(f"START {limit} {mode}", flush=True)
            embed_seconds, embed_calls, embed_inputs = 0.0, 0, 0
            real_embed = ollama.embed

            def measured_embed(texts):
                nonlocal embed_seconds, embed_calls, embed_inputs
                start = perf_counter()
                try:
                    return real_embed(texts)
                finally:
                    embed_seconds += perf_counter() - start
                    embed_calls += 1
                    embed_inputs += len(texts)

            class TimedIndex(ReviewIndex):
                def update_sources(self, model, sources, progress=lambda message: None):
                    start = perf_counter()
                    row["pre_index_seconds"] = start - review_start
                    row["source_paths"] = [s["path"] for s in sources]
                    row["source_bytes"] = sum(len(s["text"].encode()) for s in sources)
                    row["source_sha256"] = sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
                    snapshot, stats = super().update_sources(model, sources, progress)
                    row.update(index_seconds=perf_counter() - start, files=len(sources),
                               chunks=len(snapshot.chunks), **stats,
                               embed_seconds=embed_seconds, embed_calls=embed_calls,
                               embed_inputs=embed_inputs)
                    row["snapshot_bytes"] = sum(p.stat().st_size for p in snapshot.directory.rglob("*") if p.is_file())
                    row["embedding_cache_bytes"] = self.cache.stat().st_size
                    save()
                    print("INDEX " + json.dumps({k: v for k, v in row.items() if k != "source_paths"}), flush=True)
                    return snapshot, stats

            try:
                # Warm the actual embedding model outside the timed section.
                warmup = perf_counter()
                real_embed(["Index benchmark warmup"])
                row["model_warmup_seconds"] = perf_counter() - warmup
                ollama.embed = measured_embed
                review.ReviewIndex = TimedIndex
                review_start = perf_counter()
                report = await review.run_review(config, ollama, config.github_repo, 1,
                    OUTPUT / f"{limit}-{mode}", info["head_sha"], lambda msg: print(msg, flush=True))
                row["review_seconds"] = perf_counter() - review_start
                row["status"] = report["status"]
                row["findings"] = len(report["findings"])
                row["models"] = report["models"]
                row["limitations"] = report["limitations"]
                if report["status"] in ("failed", "stale") or "index_seconds" not in row:
                    raise RuntimeError(f"Benchmark failed: {report['limitations']}")
                if row["files"] != limit:
                    raise RuntimeError(f"Expected {limit} readable files; got {row['files']}")
                if mode == "warm" and row["embedded"] != 0:
                    raise RuntimeError("Warm run unexpectedly computed new index embeddings")
                print(f"DONE {limit} {mode}: index={row['index_seconds']:.3f}s review={row['review_seconds']:.3f}s", flush=True)
            finally:
                review.ReviewIndex = ReviewIndex
                ollama.close()
                save()
        for limit in (80, 1000):
            cold, warm = [r for r in results["runs"] if r["limit"] == limit]
            assert cold["source_sha256"] == warm["source_sha256"], "Sources changed between runs"
        cold80, cold1000 = results["runs"][:2]
        assert set(cold80["source_paths"]) <= set(cold1000["source_paths"]), "Corpora are not nested"
        results["cold_index_ratio"] = cold1000["index_seconds"] / cold80["index_seconds"]
        results["cold_index_delta_seconds"] = cold1000["index_seconds"] - cold80["index_seconds"]
        results["complete"] = True
    finally:
        results["outline_after"] = outline_state(base.repo_path)
        results["outline_unchanged"] = results["outline_before"] == results["outline_after"]
        results["finished"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    asyncio.run(main())
