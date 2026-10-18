import json
from dataclasses import replace

import httpx
import pytest

from outline_assistant.assistant import make_messages, validate_answer
from outline_assistant.chunking import Chunk
from outline_assistant.config import AppError
from outline_assistant.ollama_client import Ollama
from outline_assistant.search import Hit


def test_citation_validation():
    source = {"S1": {"path": "README.md", "lines": [2, 4]}}
    def response(ids, text="Факт", insufficient=False):
        return json.dumps({"paragraphs": [{"text": text, "sources": ids}], "insufficient": insufficient})
    text, citations, _ = validate_answer(response(["S1"]), source)
    assert text == "Факт [S1]" and citations == source
    for raw in [response(["S9"]), response([]), response(["S1"], "Факт [S77]"), "invalid JSON"]:
        with pytest.raises(AppError):
            validate_answer(raw, source)
    assert validate_answer(response([], "Нет информации", True), source)[2]


def test_context_budget_and_no_partial_sources(settings):
    settings = replace(settings, num_ctx=8192, answer_tokens=1800)
    chunks = [Chunk(str(i), "docs/test.md", "Title", 1, 2, "Я" * 1000, "h") for i in range(5)]
    messages, sources = make_messages(settings, "Вопрос", [Hit(c, 1) for c in chunks], {"branch": "main"}, {"files": ["x" * 100] * 80, "truncated": False})
    assert sum(len(m["content"].encode()) for m in messages) <= settings.num_ctx - settings.answer_tokens - 1024
    assert len(sources) < 5
    assert all(len(source["text"]) == 1000 for source in sources.values())


def test_ollama_payloads_and_failures(settings):
    requests = []
    def handle(request):
        data = json.loads(request.content)
        requests.append(data)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[1, 2]]})
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})
    client = Ollama(settings)
    client.client.close()
    client.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle))
    assert client.embed(["text"]) == [[1, 2]]
    client.chat([], {})
    assert requests[0]["truncate"] is False
    assert requests[0]["input"] == ["text"]
    assert requests[1]["think"] is False and requests[1]["stream"] is False
    client.close()
    def timeout(request):
        raise httpx.ReadTimeout("test")
    client.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(timeout))
    with pytest.raises(AppError, match="Таймаут"):
        client.embed(["text"])
    client.close()


def test_documented_urls_and_uncited_paragraphs():
    source = {"S1": {"text": "Set URL https://docs.example.com"}}
    response = {"paragraphs": [{"text": "Адрес https://docs.example.com", "sources": ["S1"]}], "insufficient": False}
    assert validate_answer(json.dumps(response), source)[1] == source
    response["paragraphs"][0]["text"] = "Адрес https://invented.invalid"
    with pytest.raises(AppError, match="URL"):
        validate_answer(json.dumps(response), source)
    response["paragraphs"] = [{"text": "Факт", "sources": ["S1"]}, {"text": "Неподтверждённый факт", "sources": []}]
    with pytest.raises(AppError, match="без подтверждающего"):
        validate_answer(json.dumps(response), source)
