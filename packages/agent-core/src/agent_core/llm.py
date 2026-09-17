"""Provider-neutral LLM abstraction with tool calling.

Three providers are shipped:

* :class:`AnthropicProvider` - Claude via the official SDK (default).
* :class:`OpenAICompatibleProvider` - any ``/v1/chat/completions`` server
  (Ollama on a local GPU, vLLM, TGI, LiteLLM gateway).
* :class:`HeuristicProvider` - deterministic keyword router used for CI, e2e
  clusters and offline demos. It never produces free-form reasoning; it selects
  a tool and summarises tool output verbatim. Policies block it in production.

A :class:`ScriptedProvider` is also provided for unit tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from agent_core.config import LLMSettings

Role = Literal["user", "assistant", "tool"]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ChatMessage(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = "end_turn"
    model: str = ""


ToolSpec = dict[str, Any]


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: LLMSettings, client: Any | None = None) -> None:
        self.model = settings.model
        self._settings = settings
        api_key = settings.api_key.get_secret_value() if settings.api_key else None
        # When api_key is None the SDK falls back to ANTHROPIC_API_KEY.
        self._client = client or AsyncAnthropic(api_key=api_key, timeout=settings.timeout_s)

    @staticmethod
    def to_anthropic_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []

        def flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for msg in messages:
            if msg.role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content,
                        "is_error": msg.is_error,
                    }
                )
                continue
            flush()
            if msg.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                blocks.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in msg.tool_calls
                )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "user", "content": msg.content})
        flush()
        return out

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "system": system,
            "messages": self.to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = list(tools)
        resp = await self._client.messages.create(**kwargs)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens
            ),
            stop_reason=str(resp.stop_reason),
            model=str(resp.model),
        )


# ------------------------------------------------------------------ OpenAI-compatible
class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, settings: LLMSettings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.base_url:
            raise ValueError("base_url is required")
        self.model = settings.model
        self._settings = settings
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key.get_secret_value()}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"), headers=headers, timeout=settings.timeout_s
        )

    @staticmethod
    def to_openai_messages(system: str, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            if msg.role == "tool":
                out.append(
                    {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
                )
            elif msg.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
                if msg.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in msg.tool_calls
                    ]
                out.append(item)
            else:
                out.append({"role": "user", "content": msg.content})
        return out

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "messages": self.to_openai_messages(system, messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            args = raw["function"].get("arguments") or "{}"
            parsed = json.loads(args) if isinstance(args, str) else dict(args)
            calls.append(ToolCall(id=raw["id"], name=raw["function"]["name"], arguments=parsed))
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            stop_reason=str(choice.get("finish_reason", "stop")),
            model=str(data.get("model", self.model)),
        )


# -------------------------------------------------------------------------- Heuristic
_WORD = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)
_SUBREDDIT = re.compile(r"\br/([A-Za-z0-9_]{2,21})\b")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text) if len(t) > 2}


class HeuristicProvider:
    """Deterministic router: picks the tool whose name/description/keywords best overlap
    the user request, then returns the tool output as the answer.

    Tools may add ``"x-keywords": [...]`` to their spec to improve routing.
    """

    name = "heuristic"

    def __init__(self, settings: LLMSettings | None = None) -> None:
        self.model = "heuristic-router-v1"

    @staticmethod
    def _score(query_tokens: set[str], spec: ToolSpec) -> int:
        corpus = " ".join(
            [
                spec["name"].replace("_", " "),
                spec.get("description", ""),
                " ".join(spec.get("x-keywords", [])),
            ]
        )
        return len(query_tokens & _tokens(corpus))

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        tool_msgs = [m for m in messages if m.role == "tool"]
        if tool_msgs:
            body = "\n\n".join(f"[{m.name}]\n{m.content}" for m in tool_msgs)
            return LLMResponse(
                text=f"Offline mode (heuristic provider). Tool results:\n\n{body}",
                model=self.model,
            )
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if not tools:
            return LLMResponse(text="No tools available to answer offline.", model=self.model)
        q = _tokens(user_text)
        candidates = [
            (self._score(q, spec), args, spec)
            for spec in tools
            if (args := self._fill_arguments(spec, user_text)) is not None
        ]
        candidates = [c for c in candidates if c[0] > 0]
        if not candidates:
            return LLMResponse(
                text="Offline mode could not map the request to a tool.", model=self.model
            )
        _, args, best = max(candidates, key=lambda c: c[0])
        return LLMResponse(
            tool_calls=[ToolCall(id="heuristic-1", name=best["name"], arguments=args)],
            stop_reason="tool_use",
            model=self.model,
        )

    @staticmethod
    def _fill_arguments(spec: ToolSpec, user_text: str) -> dict[str, Any] | None:
        """Fill required string arguments from the request; ``None`` if impossible."""
        schema = spec.get("input_schema", {})
        props: dict[str, Any] = schema.get("properties", {})
        args: dict[str, Any] = {}
        for field in schema.get("required", []):
            if props.get(field, {}).get("type") != "string":
                return None
            if "subreddit" in field:
                match = _SUBREDDIT.search(user_text)
                if not match:
                    return None
                args[field] = match.group(1)
            else:
                args[field] = user_text.strip()[:200]
        return args


# --------------------------------------------------------------------------- Scripted
class ScriptedProvider:
    """Replays pre-defined responses (unit tests)."""

    name = "scripted"

    def __init__(
        self,
        script: Sequence[LLMResponse] | Callable[[Sequence[ChatMessage]], LLMResponse],
    ) -> None:
        self.model = "scripted"
        self._script = script
        self._i = 0
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if callable(self._script):
            return self._script(messages)
        if self._i >= len(self._script):
            return LLMResponse(text="(script exhausted)")
        resp = self._script[self._i]
        self._i += 1
        return resp


def build_provider(settings: LLMSettings | None = None) -> LLMProvider:
    settings = settings or LLMSettings()
    if settings.provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    return HeuristicProvider(settings)
