from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import re
from urllib.parse import urlsplit


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

    github_repo: str = "metalice80/outline_t"
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    github_timeout: float = 30.0
    github_retries: int = 2
    github_max_pages: int = 30
    review_git_dir: Path = Path(".review-cache/git")
    review_index_dir: Path = Path(".review-cache/rag")
    review_git_url: str = ""
    review_git_timeout: float = 180.0
    review_code_include: tuple[str, ...] = ("app/**/*.ts", "app/**/*.tsx", "server/**/*.ts", "server/**/*.tsx", "shared/**/*.ts", "shared/**/*.tsx", "plugins/**/*.ts", "plugins/**/*.tsx", "app/**/*.js", "server/**/*.js", "shared/**/*.js", "plugins/**/*.js")
    review_max_files: int = 30
    review_max_file_bytes: int = 200000
    review_max_diff_bytes: int = 120000
    review_max_index_files: int = 1000
    review_max_tree_files: int = 10000
    review_max_search_files: int = 500
    review_max_batches: int = 40
    review_retries: int = 1
    review_keep_days: int = 14
    publish_enabled: bool = False
    publication_author: str = "github-actions[bot]"

    def __post_init__(self):
        for name in ("repo_path", "index_dir", "review_git_dir", "review_index_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())
        if not isinstance(self.include, (tuple, list)) or not isinstance(self.review_code_include, (tuple, list)):
            raise AppError("include и review_code_include должны быть списками шаблонов.")
        object.__setattr__(self, "include", tuple(self.include))
        object.__setattr__(self, "review_code_include", tuple(self.review_code_include))
        for directory in (self.review_git_dir, self.review_index_dir):
            if directory.is_relative_to(self.repo_path) or directory == self.index_dir or directory.is_relative_to(self.index_dir) or self.index_dir.is_relative_to(directory):
                raise AppError("Каталоги ревью должны быть вне Outline и отдельно от индекса /help.")
        if self.review_git_dir == self.review_index_dir or self.review_git_dir.is_relative_to(self.review_index_dir) or self.review_index_dir.is_relative_to(self.review_git_dir):
            raise AppError("Git-кеш и RAG-кеш должны быть раздельными каталогами.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repo):
            raise AppError("github_repo должен иметь вид owner/repository.")
        api_url = urlsplit(self.github_api_url)
        if api_url.username or api_url.password or api_url.query or api_url.fragment:
            raise AppError("Токен GitHub разрешён только в окружении, не в API URL.")
        if not api_url.hostname or not (api_url.scheme == "https" or (api_url.scheme == "http" and api_url.hostname in ("localhost", "127.0.0.1"))):
            raise AppError("GitHub API требует HTTPS (HTTP разрешён только для локальных тестов).")
        if not 0 <= self.review_retries <= 3 or not 0 <= self.github_retries <= 3:
            raise AppError("Число повторных попыток должно быть от 0 до 3.")
        if type(self.publish_enabled) is not bool:
            raise AppError("publish_enabled должен быть boolean.")
        if self.index_dir.is_relative_to(self.repo_path):
            raise AppError("index_dir должен находиться вне Outline: репозиторий только для чтения.")
        if self.chunk_chars < 128 or not 0 <= self.overlap_chars < self.chunk_chars // 2:
            raise AppError("Нужны chunk_chars >= 128 и 0 <= overlap_chars < chunk_chars / 2.")
        for name in ("top_k", "batch_size", "file_limit", "num_ctx", "answer_tokens", "github_max_pages", "review_max_files", "review_max_file_bytes", "review_max_diff_bytes", "review_max_index_files", "review_max_tree_files", "review_max_search_files", "review_max_batches", "review_keep_days"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise AppError(f"{name} должен быть положительным целым числом.")
        if self.file_limit > 500 or self.batch_size > 64:
            raise AppError("file_limit <= 500, batch_size <= 64.")
        for name in ("http_timeout", "mcp_timeout", "git_timeout", "github_timeout", "review_git_timeout"):
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
        for name in ("repo_path", "index_dir", "review_git_dir", "review_index_dir"):
            if name in data:
                data[name] = config.parent / Path(data[name]).expanduser()
        return Settings(**data)
    except (ValueError, TypeError, OSError) as exc:
        raise AppError(f"Ошибка конфигурации {config}: {exc}") from exc
