from dataclasses import dataclass
import numpy as np

from .chunking import Chunk
from .config import AppError
from .store import Snapshot, normalize


@dataclass
class Hit:
    chunk: Chunk
    score: float


def search(snapshot: Snapshot, query_vector, top_k: int) -> list[Hit]:
    if not snapshot.chunks:
        return []
    query = normalize([query_vector], 1)[0]
    if query.shape[0] != snapshot.vectors.shape[1]:
        raise AppError("Размерность вопроса не совпадает с индексом. Выполните index --rebuild.")
    scores = snapshot.vectors @ query
    order = np.argsort(-scores, kind="stable")[:top_k]
    return [Hit(snapshot.chunks[int(i)], float(scores[i])) for i in order]
