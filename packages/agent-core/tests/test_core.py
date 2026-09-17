from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from agent_core import (
    AgentHooks,
    ChatMessage,
    LLMResponse,
    Tool,
    ToolCall,
    ToolCallingAgent,
    ToolDeniedError,
    ToolRegistry,
    Usage,
)
from agent_core.config import SECRETS_DIR_ENV, LLMSettings, secrets_dir
from agent_core.http import (
    EgressDeniedError,
    RateLimiter,
    UpstreamError,
    build_http_client,
    request_json,
)
from agent_core.llm import (
    AnthropicProvider,
    HeuristicProvider,
    OpenAICompatibleProvider,
    ScriptedProvider,
    build_provider,
)
from agent_core.observability import AsyncTTLCache, redact_text, redaction_processor


class EchoArgs(BaseModel):
    text: str = Field(min_length=1, max_length=50)


async def _echo(args: EchoArgs) -> dict[str, str]:
    return {"echo": args.text}


def _registry() -> ToolRegistry:
    return ToolRegistry(
        [Tool("echo", "Echo text back", EchoArgs, _echo, keywords=("repeat", "echo"))]
    )


async def test_agent_executes_tool_then_answers() -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hola"})],
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
            LLMResponse(text="done", usage=Usage(input_tokens=20, output_tokens=3)),
        ]
    )
    agent = ToolCallingAgent(provider, _registry(), "You are helpful.")
    result = await agent.run("echo hola")
    assert result.answer == "done"
    assert result.usage.total == 38
    assert result.tool_results[0].raw == {"echo": "hola"}
    assert "<untrusted_tool_output>" in result.tool_results[0].content
    assert "untrusted_tool_output" in agent.system_prompt


async def test_invalid_arguments_are_reported_not_raised() -> None:
    registry = _registry()
    res = await registry.execute(ToolCall(id="x", name="echo", arguments={"text": ""}))
    assert res.is_error
    assert "Invalid arguments" in res.content


async def test_unknown_tool_and_duplicate_registration() -> None:
    registry = _registry()
    assert (await registry.execute(ToolCall(id="x", name="nope"))).is_error
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(Tool("echo", "dup", EchoArgs, _echo))


async def test_tool_timeout_and_exception() -> None:
    async def slow(_: EchoArgs) -> None:
        await asyncio.sleep(1)

    async def boom(_: EchoArgs) -> None:
        raise RuntimeError("kaput")

    reg = ToolRegistry(
        [Tool("slow", "slow", EchoArgs, slow), Tool("boom", "boom", EchoArgs, boom)],
        timeout_s=0.01,
    )
    assert (
        "timed out"
        in (await reg.execute(ToolCall(id="1", name="slow", arguments={"text": "a"}))).content
    )
    assert (
        "kaput"
        in (await reg.execute(ToolCall(id="2", name="boom", arguments={"text": "a"}))).content
    )


async def test_output_truncation() -> None:
    async def big(_: EchoArgs) -> str:
        return "x" * 100

    reg = ToolRegistry([Tool("big", "big", EchoArgs, big)], max_output_chars=10)
    res = await reg.execute(ToolCall(id="1", name="big", arguments={"text": "a"}))
    assert res.metadata["truncated"] is True


async def test_hook_can_deny_and_allowlist_enforced() -> None:
    async def deny(call: ToolCall) -> None:
        raise ToolDeniedError("policy says no")

    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "a"})]),
            LLMResponse(text="ok"),
        ]
    )
    agent = ToolCallingAgent(provider, _registry(), "sys", hooks=AgentHooks(before_tool=deny))
    result = await agent.run("hi")
    assert "Denied" in result.tool_results[0].content

    provider2 = ScriptedProvider(
        [
            LLMResponse(tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "a"})]),
            LLMResponse(text="ok"),
        ]
    )
    agent2 = ToolCallingAgent(provider2, _registry(), "sys", allowed_tools=set())
    result2 = await agent2.run("hi")
    assert "not permitted" in result2.tool_results[0].content


async def test_step_and_call_budgets() -> None:
    call = ToolCall(id="1", name="echo", arguments={"text": "a"})
    provider = ScriptedProvider(
        [LLMResponse(tool_calls=[call]), LLMResponse(tool_calls=[call]), LLMResponse(text="final")]
    )
    agent = ToolCallingAgent(provider, _registry(), "sys", max_steps=2, max_tool_calls=1)
    result = await agent.run("loop")
    assert result.stopped_reason == "max_steps"
    assert result.answer == "final"
    assert "budget exhausted" in result.tool_results[1].content


async def test_token_budget_stops_run() -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "a"})],
                usage=Usage(input_tokens=999, output_tokens=999),
            )
        ]
    )
    agent = ToolCallingAgent(provider, _registry(), "sys", max_total_tokens=100)
    result = await agent.run("x")
    assert result.stopped_reason == "token_budget"


async def test_heuristic_provider_routes_and_summarises() -> None:
    provider = HeuristicProvider()
    specs = _registry().specs()
    first = await provider.complete(
        system="", messages=[ChatMessage(role="user", content="please repeat this")], tools=specs
    )
    assert first.tool_calls[0].name == "echo"
    assert first.tool_calls[0].arguments == {"text": "please repeat this"}
    second = await provider.complete(
        system="",
        messages=[ChatMessage(role="tool", name="echo", content="payload", tool_call_id="1")],
        tools=specs,
    )
    assert "payload" in second.text
    miss = await provider.complete(
        system="", messages=[ChatMessage(role="user", content="zzz qqq")], tools=specs
    )
    assert not miss.tool_calls
    none = await provider.complete(system="", messages=[], tools=[])
    assert "No tools" in none.text


async def test_heuristic_subreddit_extraction() -> None:
    class SubArgs(BaseModel):
        subreddit: str

    async def h(_: SubArgs) -> str:
        return "ok"

    specs = ToolRegistry([Tool("subreddit_posts", "hot posts", SubArgs, h)]).specs()
    p = HeuristicProvider()
    hit = await p.complete(
        system="", messages=[ChatMessage(role="user", content="hot posts in r/python")], tools=specs
    )
    assert hit.tool_calls[0].arguments == {"subreddit": "python"}
    miss = await p.complete(
        system="", messages=[ChatMessage(role="user", content="hot posts")], tools=specs
    )
    assert not miss.tool_calls


def test_anthropic_message_conversion_groups_tool_results() -> None:
    msgs = [
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="assistant",
            content="thinking",
            tool_calls=[ToolCall(id="a", name="t", arguments={}), ToolCall(id="b", name="t")],
        ),
        ChatMessage(role="tool", content="r1", tool_call_id="a", name="t"),
        ChatMessage(role="tool", content="r2", tool_call_id="b", name="t", is_error=True),
    ]
    out = AnthropicProvider.to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert len(out[2]["content"]) == 2
    assert out[2]["content"][1]["is_error"] is True


async def test_anthropic_provider_parses_blocks() -> None:
    class Block:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    class FakeMessages:
        async def create(self, **kwargs: Any) -> Any:
            assert kwargs["tools"][0]["name"] == "echo"
            return Block(
                content=[
                    Block(type="text", text="hi"),
                    Block(type="tool_use", id="t1", name="echo", input={"text": "x"}),
                ],
                usage=Block(input_tokens=3, output_tokens=4),
                stop_reason="tool_use",
                model="claude-sonnet-5",
            )

    class FakeClient:
        messages = FakeMessages()

    provider = AnthropicProvider(LLMSettings(provider="anthropic"), client=FakeClient())
    resp = await provider.complete(
        system="s", messages=[ChatMessage(role="user", content="q")], tools=_registry().specs()
    )
    assert resp.text == "hi"
    assert resp.tool_calls[0].arguments == {"text": "x"}
    assert resp.usage.total == 7


async def test_openai_compatible_provider_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert '"tools"' in body
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text":"y"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    settings = LLMSettings(
        provider="openai_compatible", base_url="http://localhost:11434/v1", api_key="k"
    )
    client = httpx.AsyncClient(
        base_url="http://localhost:11434/v1",
        headers={"Authorization": "Bearer k"},
        transport=httpx.MockTransport(handler),
    )
    provider = OpenAICompatibleProvider(settings, client=client)
    history = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c0", name="echo")]),
        ChatMessage(role="tool", content="r", tool_call_id="c0"),
    ]
    resp = await provider.complete(system="s", messages=history, tools=_registry().specs())
    assert resp.tool_calls[0].arguments == {"text": "y"}
    assert resp.usage.total == 7


def test_settings_validation_and_factory() -> None:
    with pytest.raises(ValueError, match="BASE_URL is required"):
        LLMSettings(provider="openai_compatible")
    with pytest.raises(ValueError, match="https"):
        LLMSettings(provider="openai_compatible", base_url="http://evil.example.com")
    assert build_provider(LLMSettings(provider="heuristic")).name == "heuristic"
    assert (
        build_provider(
            LLMSettings(provider="openai_compatible", base_url="https://llm.internal/v1")
        ).name
        == "openai_compatible"
    )
    assert build_provider(LLMSettings(provider="anthropic", api_key="x")).name == "anthropic"


@pytest.mark.security
async def test_egress_allowlist_blocks_ssrf() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    client = build_http_client(
        allowed_hosts=["api.example.com"], user_agent="t/1", transport=transport
    )
    assert await request_json(client, "GET", "https://api.example.com/x") == {"ok": True}
    with pytest.raises(EgressDeniedError):
        await request_json(client, "GET", "https://169.254.169.254/latest/meta-data")
    with pytest.raises(EgressDeniedError):
        await request_json(client, "GET", "http://api.example.com/x")
    with pytest.raises(ValueError, match="empty"):
        build_http_client(allowed_hosts=[], user_agent="t")


async def test_request_json_retries_then_fails() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        if request.url.path == "/bad":
            return httpx.Response(404, text="missing")
        return httpx.Response(200, json=[1])

    client = build_http_client(
        allowed_hosts=["api.example.com"],
        user_agent="t/1",
        transport=httpx.MockTransport(handler),
    )
    assert await request_json(client, "GET", "https://api.example.com/ok") == [1]
    assert calls["n"] == 2
    with pytest.raises(UpstreamError) as exc:
        await request_json(client, "GET", "https://api.example.com/bad")
    assert exc.value.status == 404


async def test_rate_limiter_blocks_when_empty() -> None:
    limiter = RateLimiter(rate=1, per=0.05)
    await limiter.acquire()
    start = asyncio.get_running_loop().time()
    await limiter.acquire()
    assert asyncio.get_running_loop().time() - start >= 0.03


@pytest.mark.security
def test_redaction() -> None:
    event = {
        "api_key": "abc",
        "nested": {"Authorization": "Bearer xyz"},
        "msg": "token sk-abcdefghijklmnopqrstuv and Bearer abc.def",
        "items": ["ghp_" + "a" * 36],
    }
    out = redaction_processor(None, "info", event)
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"]["Authorization"] == "***REDACTED***"
    assert "sk-" not in out["msg"]
    assert "ghp_" not in out["items"][0]
    assert redact_text("AKIAABCDEFGHIJKLMNOP") == "***REDACTED***"


async def test_ttl_cache_single_flight() -> None:
    cache: AsyncTTLCache[int] = AsyncTTLCache(ttl_s=60, max_items=1)
    calls = {"n": 0}

    async def factory() -> int:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return calls["n"]

    results = await asyncio.gather(*(cache.get_or_set("k", factory) for _ in range(5)))
    assert results == [1] * 5
    assert await cache.get_or_set("k2", factory) == 2  # evicts k
    cache.clear()
    assert await cache.get_or_set("k", factory) == 3


def test_secrets_dir_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(SECRETS_DIR_ENV, raising=False)
    assert secrets_dir() is None
    monkeypatch.setenv(SECRETS_DIR_ENV, str(tmp_path / "missing"))
    assert secrets_dir() is None
    monkeypatch.setenv(SECRETS_DIR_ENV, str(tmp_path))
    assert secrets_dir() == str(tmp_path)
