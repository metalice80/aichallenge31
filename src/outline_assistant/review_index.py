"""Snapshot RAG reuses the existing atomic SQLite/NumPy generation store."""
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3

import numpy as np

from .code_chunks import PARSER_VERSION, split_source
from .config import AppError
from .store import Index, normalize


class ReviewIndex(Index):
    def __init__(self, settings, repo, number, head):
        self.review_settings = settings
        directory = settings.review_index_dir / "snapshots" / repo.replace("/", "--") / f"{number}-{head}"
        super().__init__(settings)
        self.root = directory
        self.cache = settings.review_index_dir / "embeddings.sqlite3"
        self.repo, self.head = repo, head

    def update_sources(self, ollama, sources, progress=lambda message: None):
        with self.locked():
            identity = ollama.identity(self.settings.embedding_model)
            signature = {"model": identity, "parser": PARSER_VERSION, "size": self.settings.chunk_chars, "overlap": self.settings.overlap_chars, "repo": self.repo, "sha": self.head}
            hashes = {s["path"]: sha256((s["text"] + str(s.get("changed", False))).encode()).hexdigest() for s in sources}
            old = self.load()
            if old and old.metadata["signature"] == signature and old.documents == hashes:
                return old, {"embedded": 0, "reused": len(old.chunks)}
            chunks = [c for source in sources for c in split_source(source["path"], source["text"], source["kind"], self.head, self.settings.chunk_chars, self.settings.overlap_chars, source.get("changed", False))]
            model_key = json.dumps({k: v for k, v in signature.items() if k not in ("repo", "sha")}, sort_keys=True)
            keys = [sha256((model_key + "\0" + c.text).encode()).hexdigest() for c in chunks]
            vectors, pending, reused = {}, {}, 0
            with sqlite3.connect(self.cache, timeout=30) as db:
                db.execute("CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY, dimension INTEGER NOT NULL, vector BLOB NOT NULL)")
                for key, chunk in zip(keys, chunks):
                    row = db.execute("SELECT dimension,vector FROM embeddings WHERE key=?", (key,)).fetchone()
                    if row:
                        vector = np.frombuffer(row[1], dtype=np.float32)
                        if len(vector) != row[0]:
                            raise AppError("Повреждён кеш embeddings; очистите review RAG кеш.")
                        vectors[key] = normalize([vector], 1)[0]
                        reused += 1
                    else:
                        pending[key] = chunk.text
                pending_items = list(pending.items())
                for start in range(0, len(pending_items), self.settings.batch_size):
                    batch = pending_items[start:start + self.settings.batch_size]
                    progress(f"Review embeddings: {start + 1}–{start + len(batch)} / {len(pending_items)}")
                    matrix = normalize(ollama.embed([text for _, text in batch]), len(batch))
                    for (key, _), vector in zip(batch, matrix, strict=True):
                        vectors[key] = vector
                        db.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)", (key, len(vector), vector.tobytes()))
                    # Atomic cache entries are reusable even if a later batch fails.
                    db.commit()
            if ollama.identity(self.settings.embedding_model) != identity:
                raise AppError("Embedding-модель изменилась при построении снимка.")
            dimensions = {len(v) for v in vectors.values()}
            if len(dimensions) > 1:
                raise AppError("Несовместимая размерность кеша embeddings. Очистите кеш ревью.")
            matrix = np.stack([vectors[k] for k in keys]) if keys else np.empty((0, 0), dtype=np.float32)
            self.publish(chunks, matrix, hashes, signature)
            return self.load(), {"embedded": len(pending), "reused": reused}
