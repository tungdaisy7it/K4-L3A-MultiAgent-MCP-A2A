from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5


class ToolCallError(RuntimeError):
    """The MCP tool executed and reported an error (for example: no record in scope)."""


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        # Tools are read-only, so retrying a transport failure is idempotent. Tool errors are
        # final answers from the gateway and are never retried.
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except Exception:
                if attempt == MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        # mcp>=2 exposes snake_case attributes; keep the camelCase fallback for older clients.
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise ToolCallError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
