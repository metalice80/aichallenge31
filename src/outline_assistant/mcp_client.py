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

    async def tool_schemas(self, allowed):
        result = await self.session.list_tools()
        return [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
                for t in result.tools if t.name in allowed]

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
async def connect(settings: Settings, review=False, task_run_id=None):
    env = {}
    if review and task_run_id:
        raise AppError("Task and review modes are separate.")
    if task_run_id:
        env["OUTLINE_TASK_SETTINGS"] = json.dumps(asdict(settings), default=str)
        env["OUTLINE_TASK_RUN_ID"] = task_run_id
    if review:
        env["OUTLINE_REVIEW_SETTINGS"] = json.dumps(asdict(settings), default=str)
        for name in ("GITHUB_TOKEN", "GH_TOKEN"):
            if name in os.environ:
                env[name] = os.environ[name]
    parameters = StdioServerParameters(command=sys.executable, env=env, args=["-m", "outline_assistant.mcp_server", "--repo", str(settings.repo_path), "--git-timeout", str(settings.git_timeout)])
    try:
        async with stdio_client(parameters) as (read, write):
            timeout = settings.task_tool_timeout if task_run_id else max(settings.mcp_timeout, settings.review_git_timeout + 10) if review else settings.mcp_timeout
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                async with asyncio.timeout(settings.mcp_timeout):
                    await session.initialize()
                yield ProjectContext(session)
    except Exception as exc:
        if isinstance(exc, AppError):
            raise
        # AnyIO wraps errors raised by the caller inside the yielded session.
        # A model-loop/application error is not a transport or timeout failure.
        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]
        causes = leaves(exc)
        if len(causes) == 1 and isinstance(causes[0], AppError):
            raise causes[0] from exc
        def details(error):
            if isinstance(error, BaseExceptionGroup):
                return "; ".join(details(e) for e in error.exceptions)
            return str(error) if isinstance(error, AppError) else type(error).__name__
        raise AppError(f"Ошибка MCP: {details(exc)}. Проверьте repo_path, Git и установку пакета; при необходимости увеличьте mcp_timeout.") from exc
