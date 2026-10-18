import httpx

from .config import AppError, Settings


class Ollama:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(base_url=settings.ollama_url.rstrip("/"), timeout=settings.http_timeout, trust_env=False)

    def close(self):
        self.client.close()

    def request(self, endpoint: str, data=None):
        try:
            response = self.client.get(endpoint) if data is None else self.client.post(endpoint, json=data)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise AppError("Ollama вернула JSON неверного типа.")
            if result.get("error"):
                raise AppError(f"Ollama: {result['error']}")
            return result
        except httpx.TimeoutException as exc:
            raise AppError("Таймаут Ollama. Проверьте ollama ps или увеличьте http_timeout.") from exc
        except httpx.HTTPError as exc:
            detail = exc.response.text[:500] if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            raise AppError(f"Ollama недоступна или отклонила запрос: {detail}. Проверьте ollama serve, адрес и модели; при превышении контекста уменьшите chunk_chars.") from exc
        except ValueError as exc:
            raise AppError("Ollama вернула некорректный JSON.") from exc

    def identity(self, model: str):
        name = model if ":" in model else model + ":latest"
        for item in self.request("/api/tags").get("models", []):
            if item.get("name") == name:
                return {"name": name, "digest": item["digest"]}
        raise AppError(f"Нет модели {model}. Установите её: ollama pull {model}. Автозамена отключена.")

    def embed(self, texts: list[str]):
        result = self.request("/api/embed", {"model": self.settings.embedding_model, "input": texts, "truncate": False})
        if "embeddings" not in result:
            raise AppError("Ollama не вернула embeddings.")
        return result["embeddings"]

    def chat(self, messages: list[dict], schema: dict):
        result = self.request("/api/chat", {
            "model": self.settings.chat_model, "messages": messages,
            "think": False, "stream": False, "format": schema,
            "options": {"num_ctx": self.settings.num_ctx, "num_predict": self.settings.answer_tokens, "temperature": 0},
        })
        if result.get("done_reason") == "length":
            raise AppError("Ответ модели достиг лимита. Увеличьте answer_tokens или уточните вопрос.")
        if not result.get("message", {}).get("content"):
            raise AppError("Ollama вернула пустой ответ.")
        return result["message"]["content"]

    def tool_chat(self, messages: list[dict], tools: list[dict]):
        """Native Ollama tool calls; keep existing structured-answer API unchanged."""
        result = self.request("/api/chat", {
            "model": self.settings.chat_model, "messages": messages, "tools": tools,
            "think": False, "stream": False,
            "options": {"num_ctx": self.settings.num_ctx, "num_predict": self.settings.answer_tokens, "temperature": 0},
        })
        if result.get("done_reason") == "length":
            raise AppError("Tool response reached answer_tokens; incomplete call discarded.")
        message = result.get("message")
        if not isinstance(message, dict):
            raise AppError("Ollama: отсутствует tool message.")
        # Never retain hidden thinking, even if a server unexpectedly returns it.
        return {"role": "assistant", "content": message.get("content", ""), "tool_calls": message.get("tool_calls", [])}
