import asyncio
import os
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import AppError, Settings


class ProjectContext:
    def __init__(self, session):
        self.session = session

    async def call(self, name: str, arguments=None):
        result = await self.session.call_tool(name, arguments or {})
        if result.isError:
            detail = " ".join(item.text for item in result.content if item.type == "text")
            raise AppError(f"MCP {name}: {detail}")
        if result.structuredContent is not None:
            return result.structuredContent
        texts = [item.text for item in result.content if item.type == "text"]
        if not texts:
            raise AppError(f"MCP {name}: нет данных")
        return json.loads(texts[0])


@asynccontextmanager
async def connect(settings: Settings, review=False):
    env = {}
    if review:
        env["OUTLINE_REVIEW_SETTINGS"] = json.dumps(asdict(settings), default=str)
        for name in ("GITHUB_TOKEN", "GH_TOKEN"):
            if name in os.environ:
                env[name] = os.environ[name]
    parameters = StdioServerParameters(command=sys.executable, env=env, args=["-m", "outline_assistant.mcp_server", "--repo", str(settings.repo_path), "--git-timeout", str(settings.git_timeout)])
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=max(settings.mcp_timeout, settings.review_git_timeout + 10) if review else settings.mcp_timeout)) as session:
                async with asyncio.timeout(settings.mcp_timeout):
                    await session.initialize()
                yield ProjectContext(session)
    except Exception as exc:
        if isinstance(exc, AppError):
            raise
        def details(error):
            if isinstance(error, BaseExceptionGroup):
                return "; ".join(details(e) for e in error.exceptions)
            return str(error) if isinstance(error, AppError) else type(error).__name__
        raise AppError(f"Ошибка MCP: {details(exc)}. Проверьте repo_path, Git и установку пакета; при необходимости увеличьте mcp_timeout.") from exc
