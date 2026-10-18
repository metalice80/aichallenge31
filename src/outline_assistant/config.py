from dataclasses import dataclass, field
from pathlib import Path
import tomllib


class AppError(Exception):
    """An actionable application error, safe to display in the CLI."""


@dataclass(frozen=True)
class Settings:
    repo_path: Path = Path("/Users/sergey/ai/aichallenge/outline")
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "bge-m3"
    chat_model: str = "qwen3:8b"
    index_dir: Path = Path(".index")
    include: tuple[str, ...] = ("README.md", "docs/**/*.md")
    chunk_chars: int = 1400
    overlap_chars: int = 160
    top_k: int = 5
    batch_size: int = 8
    http_timeout: float = 180.0
    mcp_timeout: float = 30.0
    git_timeout: float = 10.0
    file_limit: int = 80
    num_ctx: int = 16384
    answer_tokens: int = 1800

    def __post_init__(self):
        for name in ("repo_path", "index_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())
        object.__setattr__(self, "include", tuple(self.include))
        if self.index_dir.is_relative_to(self.repo_path):
            raise AppError("index_dir должен находиться вне Outline: репозиторий только для чтения.")
        if self.chunk_chars < 128 or not 0 <= self.overlap_chars < self.chunk_chars // 2:
            raise AppError("Нужны chunk_chars >= 128 и 0 <= overlap_chars < chunk_chars / 2.")
        for name in ("top_k", "batch_size", "file_limit", "num_ctx", "answer_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise AppError(f"{name} должен быть положительным целым числом.")
        if self.file_limit > 500 or self.batch_size > 64:
            raise AppError("file_limit <= 500, batch_size <= 64.")
        for name in ("http_timeout", "mcp_timeout", "git_timeout"):
            if not 0 < getattr(self, name) <= 3600:
                raise AppError(f"{name} должен быть в интервале (0, 3600].")
        if self.num_ctx - self.answer_tokens < 4096:
            raise AppError("Оставьте не менее 4096 позиций контекста для входных данных.")
        if not self.include or any(not isinstance(p, str) or p.startswith("/") or ".." in p.split("/") for p in self.include):
            raise AppError("include: нужны относительные шаблоны без '..'.")
        if not self.ollama_url.startswith(("http://", "https://")):
            raise AppError("ollama_url должен начинаться с http:// или https://.")


def load_settings(path: str | None = None) -> Settings:
    config = Path(path or "config.toml").expanduser().resolve()
    if not config.exists():
        if path:
            raise AppError(f"Нет конфигурации {config}. Скопируйте config.example.toml.")
        return Settings()
    try:
        data = tomllib.loads(config.read_text())
        for name in ("repo_path", "index_dir"):
            if name in data:
                data[name] = config.parent / Path(data[name]).expanduser()
        return Settings(**data)
    except (ValueError, TypeError, OSError) as exc:
        raise AppError(f"Ошибка конфигурации {config}: {exc}") from exc
