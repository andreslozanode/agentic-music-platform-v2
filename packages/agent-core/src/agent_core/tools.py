"""Typed tool registry.

Every tool declares a Pydantic argument model, so the JSON schema sent to the LLM
and the validation applied to the LLM's arguments come from a single source of truth.
Tool output is always treated as *untrusted data* and wrapped accordingly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from agent_core.llm import ToolCall, ToolSpec

RiskLevel = Literal["low", "medium", "high"]

UNTRUSTED_OPEN = "<untrusted_tool_output>"
UNTRUSTED_CLOSE = "</untrusted_tool_output>"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[Any]]
    risk: RiskLevel = "low"
    keywords: tuple[str, ...] = ()

    def spec(self) -> ToolSpec:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        spec: ToolSpec = {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }
        if self.keywords:
            spec["x-keywords"] = list(self.keywords)
        return spec


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _serialise(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, list) and value and all(isinstance(v, BaseModel) for v in value):
        return json.dumps([v.model_dump(mode="json") for v in value], ensure_ascii=False)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        timeout_s: float = 30.0,
        max_output_chars: int = 12_000,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        return [t.spec() for n, t in sorted(self._tools.items()) if allowed is None or n in allowed]

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"Unknown tool '{call.name}'", is_error=True)
        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_input=False)
            msg = f"Invalid arguments: {json.dumps(errors, default=str)}"
            return ToolResult(call.id, call.name, msg, is_error=True)
        try:
            raw = await asyncio.wait_for(tool.handler(args), timeout=self.timeout_s)
        except TimeoutError:
            return ToolResult(call.id, call.name, "Tool timed out", is_error=True)
        except Exception as exc:  # tool failures are reported to the model, not raised
            return ToolResult(
                call.id, call.name, f"Tool failed: {type(exc).__name__}: {exc}", is_error=True
            )
        text = _serialise(raw)
        truncated = len(text) > self.max_output_chars
        if truncated:
            text = text[: self.max_output_chars] + "...[truncated]"
        wrapped = f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"
        return ToolResult(call.id, call.name, wrapped, raw=raw, metadata={"truncated": truncated})
