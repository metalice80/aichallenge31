from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import uuid

import numpy as np

from .chunking import Chunk, split_document
from .config import AppError, Settings
from .documents import read_documents


def normalize(values, expected_rows=None) -> np.ndarray:
    try:
        matrix = np.asarray(values, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[1] == 0 or not np.isfinite(matrix).all():
            raise ValueError("некорректная форма, NaN или Inf")
        if expected_rows is not None and len(matrix) != expected_rows:
            raise ValueError("неверное число векторов")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms <= 0):
            raise ValueError("нулевой или слишком большой вектор")
        return matrix / norms
    except (TypeError, ValueError) as exc:
        raise AppError(f"Некорректные embeddings: {exc}.") from exc


class DimensionChanged(AppError):
    """Embedding dimensions changed without a new model digest."""


@dataclass
class Snapshot:
    directory: Path
    metadata: dict
    vectors: np.ndarray
    chunks: list[Chunk]
    documents: dict[str, str]


def checksum(path: Path):
    return sha256(path.read_bytes()).hexdigest()


def sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Index:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.index_dir

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AppError("Другой процесс обновляет индекс. Повторите после его завершения.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self) -> Snapshot | None:
        pointer = self.root / "CURRENT"
        if not pointer.exists():
            return None
        try:
            name = pointer.read_text().strip()
            if not name.startswith("gen-") or Path(name).name != name:
                raise ValueError("неверный указатель поколения")
            directory = self.root / name
            metadata = json.loads((directory / "manifest.json").read_text())
            for filename, expected in metadata["checksums"].items():
                if filename not in ("chunks.sqlite3", "vectors.npy") or checksum(directory / filename) != expected:
                    raise ValueError("контрольная сумма поколения не совпадает")
            if set(metadata["checksums"]) != {"chunks.sqlite3", "vectors.npy"}:
                raise ValueError("нет контрольных сумм")
            vectors = np.load(directory / "vectors.npy", allow_pickle=False)
            with sqlite3.connect(f"{(directory / 'chunks.sqlite3').as_uri()}?mode=ro", uri=True) as db:
                columns = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
                extras = ",source_type,snapshot_sha,symbol,fallback,changed" if "source_type" in columns else ""
                rows = db.execute("SELECT row_index,id,path,heading,start_line,end_line,text,hash" + extras + " FROM chunks ORDER BY row_index").fetchall()
                documents = dict(db.execute("SELECT path,hash FROM documents"))
            if [r[0] for r in rows] != list(range(len(rows))):
                raise ValueError("нарушена связь матрицы и фрагментов")
            chunks = [Chunk(*row[1:]) for row in rows]
            if vectors.shape != (len(chunks), metadata["dimension"]):
                raise ValueError("размерность матрицы не совпадает")
            if chunks:
                normalized = normalize(vectors, len(chunks))
                if not np.allclose(normalized, vectors, atol=1e-5):
                    raise ValueError("матрица не нормализована")
            return Snapshot(directory, metadata, vectors, chunks, documents)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, AppError) as exc:
            raise AppError(f"Индекс повреждён: {exc}. Выполните index --rebuild.") from exc

    def publish(self, chunks, vectors, documents, signature):
        directory = self.root / ("gen-" + uuid.uuid4().hex)
        directory.mkdir()
        # A failure leaves an unreachable generation; CURRENT is replaced only at the end.
        with sqlite3.connect(directory / "chunks.sqlite3") as db:
            db.executescript("""
                CREATE TABLE documents(path TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE chunks(row_index INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                    path TEXT NOT NULL, heading TEXT NOT NULL, start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL, text TEXT NOT NULL, hash TEXT NOT NULL,
                    source_type TEXT NOT NULL, snapshot_sha TEXT NOT NULL, symbol TEXT NOT NULL,
                    fallback INTEGER NOT NULL, changed INTEGER NOT NULL);
            """)
            db.executemany("INSERT INTO documents VALUES (?,?)", documents.items())
            db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [(i, c.id, c.path, c.heading, c.start_line, c.end_line, c.text, c.hash, c.source_type, c.snapshot_sha, c.symbol, c.fallback, c.changed) for i, c in enumerate(chunks)])
        with (directory / "vectors.npy").open("wb") as output:
            np.save(output, vectors, allow_pickle=False)
            output.flush()
            os.fsync(output.fileno())
        metadata = {"signature": signature, "dimension": vectors.shape[1], "count": len(chunks), "checksums": {name: checksum(directory / name) for name in ("chunks.sqlite3", "vectors.npy")}}
        (directory / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        for filename in ("chunks.sqlite3", "manifest.json"):
            with (directory / filename).open("rb") as handle:
                os.fsync(handle.fileno())
        sync_directory(directory)
        temporary = self.root / ("CURRENT-" + uuid.uuid4().hex)
        with temporary.open("w") as handle:
            handle.write(directory.name + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.root / "CURRENT")
        sync_directory(self.root)

    def update(self, ollama, progress=lambda message: None, rebuild=False):
        try:
            return self._update(ollama, progress, rebuild)
        except DimensionChanged:
            if rebuild:
                raise
            progress("Изменилась размерность модели; полностью перестраиваю индекс…")
            return self._update(ollama, progress, rebuild=True)

    def _update(self, ollama, progress, rebuild):
        with self.locked():
            documents = read_documents(self.settings)
            hashes = {doc.path: doc.hash for doc in documents}
            identity = ollama.identity(self.settings.embedding_model)
            signature = {"version": 1, "repo_path": str(self.settings.repo_path), "model": identity, "include": self.settings.include, "chunk_chars": self.settings.chunk_chars, "overlap_chars": self.settings.overlap_chars}
            # JSON canonicalization ensures tuples compare equally after loading.
            signature = json.loads(json.dumps(signature))
            old = None if rebuild else self.load()
            compatible = old is not None and old.metadata["signature"] == signature
            old_hashes = old.documents if compatible else {}
            changed = {path for path, digest in hashes.items() if old_hashes.get(path) != digest}
            removed = set(old_hashes) - set(hashes)
            if compatible and not changed and not removed:
                return old, {"changed": 0, "removed": 0, "embedded": 0, "reused": len(old.chunks)}
            chunks, vector_rows = [], []
            if compatible:
                for chunk, vector in zip(old.chunks, old.vectors, strict=True):
                    if chunk.path in hashes and chunk.path not in changed:
                        chunks.append(chunk)
                        vector_rows.append(vector)
            reused = len(chunks)
            pending = [chunk for doc in documents if doc.path in changed for chunk in split_document(doc, self.settings.chunk_chars, self.settings.overlap_chars)]
            dimension = (old.metadata["dimension"] or None) if compatible else None
            for start in range(0, len(pending), self.settings.batch_size):
                batch = pending[start:start + self.settings.batch_size]
                progress(f"Embeddings: {start + 1}–{start + len(batch)} / {len(pending)}")
                vectors = normalize(ollama.embed([chunk.text for chunk in batch]), len(batch))
                if dimension is not None and dimension != vectors.shape[1]:
                    # A server changed dimensions without changing its advertised digest.
                    raise DimensionChanged("Модель вернула несогласованные размерности embeddings.")
                dimension = vectors.shape[1]
                chunks.extend(batch)
                vector_rows.extend(vectors)
            if {doc.path: doc.hash for doc in read_documents(self.settings)} != hashes:
                raise AppError("Документы изменились во время индексации. Повторите запрос.")
            if ollama.identity(self.settings.embedding_model) != identity:
                raise AppError("Модель изменилась во время индексации. Повторите запрос.")
            matrix = np.asarray(vector_rows, dtype=np.float32) if vector_rows else np.empty((0, dimension or 0), dtype=np.float32)
            self.publish(chunks, matrix, hashes, signature)
            return self.load(), {"changed": len(changed), "removed": len(removed), "embedded": len(pending), "reused": reused}
