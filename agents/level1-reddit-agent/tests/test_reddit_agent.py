from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from agent_core import LLMResponse, ToolCall
from agent_core.llm import HeuristicProvider, ScriptedProvider
from reddit_agent.agent import RedditTools, SearchArgs, SubredditPostsArgs, build_agent
from reddit_agent.cli import app
from reddit_agent.client import (
    FixtureRedditSource,
    RedditOAuthClient,
    build_source,
    validate_post_id,
    validate_subreddit,
)
from reddit_agent.config import RedditSettings

LISTING = {
    "kind": "Listing",
    "data": {
        "children": [
            {
                "kind": "t3",
                "data": {
                    "id": "abc123",
                    "subreddit": "python",
                    "title": "Hello",
                    "author": "someone",
                    "score": 10,
                    "num_comments": 2,
                    "created_utc": 1789600000,
                    "permalink": "/r/python/comments/abc123/hello/",
                    "selftext": "x" * 2000,
                    "over_18": False,
                },
            },
            {
                "kind": "t3",
                "data": {
                    "id": "nsfw01",
                    "subreddit": "python",
                    "title": "Hidden",
                    "created_utc": 1789600000,
                    "permalink": "/r/python/comments/nsfw01/",
                    "over_18": True,
                },
            },
        ]
    },
}

COMMENTS = [
    LISTING,
    {
        "data": {
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "id": "c1",
                        "author": "a",
                        "body": "top",
                        "score": 5,
                        "replies": {
                            "data": {
                                "children": [
                                    {"kind": "t1", "data": {"id": "c2", "body": "reply"}},
                                    {"kind": "more", "data": {}},
                                ]
                            }
                        },
                    },
                }
            ]
        }
    },
]


def _settings(**kw: object) -> RedditSettings:
    base: dict[str, object] = {"client_id": "id", "client_secret": "secret", "mode": "api"}
    base.update(kw)
    return RedditSettings(**base)  # type: ignore[arg-type]


def _transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.reddit.com":
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(
                200, json={"data": {"display_name": "python", "title": "Python", "subscribers": 1}}
            )
        if "/comments/" in path:
            return httpx.Response(200, json=COMMENTS)
        return httpx.Response(200, json=LISTING)

    return httpx.MockTransport(handler)


async def test_oauth_client_flow_and_token_reuse() -> None:
    seen: list[httpx.Request] = []
    client = RedditOAuthClient(_settings(), transport=_transport(seen))
    posts = await client.subreddit_posts("r/python", "top", "week", 5)
    assert posts[0].permalink == "https://www.reddit.com/r/python/comments/abc123/hello/"
    assert posts[0].selftext.endswith("…")
    await client.search("hello", "python", "month", 5)
    await client.search("hello", None, "month", 5)
    comments = await client.comments("python", "t3_ABC123", 10)
    assert [c.depth for c in comments] == [0, 1]
    info = await client.subreddit_info("python")
    assert info.title == "Python"
    token_calls = [r for r in seen if r.url.host == "www.reddit.com"]
    assert len(token_calls) == 1
    assert all("raw_json=1" in str(r.url) for r in seen if r.url.host == "oauth.reddit.com")
    search_restricted = next(r for r in seen if r.url.path == "/r/python/search")
    assert "restrict_sr=1" in str(search_restricted.url)
    await client.aclose()


@pytest.mark.security
@pytest.mark.parametrize("bad", ["../etc", "a", "python/../../x", "x" * 30, "py thon"])
def test_subreddit_validation_blocks_path_injection(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid subreddit"):
        validate_subreddit(bad)


@pytest.mark.security
def test_post_id_validation() -> None:
    assert validate_post_id("t3_AbC1") == "abc1"
    with pytest.raises(ValueError, match="invalid post id"):
        validate_post_id("abc/../../")


def test_settings_guards() -> None:
    with pytest.raises(ValueError, match=r"User-Agent|USER_AGENT"):
        _settings(user_agent="python-requests/2.0")
    with pytest.raises(ValueError, match="not allowed in prod"):
        RedditSettings(mode="fixtures", ENVIRONMENT="prod")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="required"):
        RedditOAuthClient(RedditSettings(mode="api"))


async def test_tools_filter_nsfw_and_cap_results() -> None:
    seen: list[httpx.Request] = []
    client = RedditOAuthClient(_settings(max_results=3), transport=_transport(seen))
    tools = RedditTools(client, _settings(max_results=3))
    posts = await tools.subreddit_posts(SubredditPostsArgs(subreddit="python", limit=25))
    assert [p.id for p in posts] == ["abc123"]
    assert "limit=3" in str(seen[-1].url)
    permissive = RedditTools(client, _settings(include_nsfw=True))
    assert len(await permissive.search(SearchArgs(query="hello"))) == 2
    await client.aclose()


async def test_fixture_source_behaviour() -> None:
    src = FixtureRedditSource()
    top = await src.subreddit_posts("python", "top", "week", 5)
    assert top[0].score >= top[-1].score
    new = await src.subreddit_posts("dataengineering", "new", "week", 5)
    assert new[0].created_utc >= new[-1].created_utc
    found = await src.search("lakehouse catalog", None, "month", 5)
    assert found
    assert found[0].id == "1a2b3c"
    assert await src.search("lakehouse", "python", "month", 5) == []
    assert len(await src.comments("dataengineering", "1a2b3c", 10)) == 2
    assert (await src.subreddit_info("python")).name == "python"
    with pytest.raises(LookupError):
        await src.subreddit_info("nonexistent")
    await src.aclose()
    assert isinstance(build_source(RedditSettings(mode="fixtures")), FixtureRedditSource)


async def test_agent_end_to_end_with_scripted_llm() -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="search_reddit",
                        arguments={"query": "prompt injection", "limit": 3},
                    )
                ]
            ),
            LLMResponse(text="Found a relevant thread."),
        ]
    )
    agent, src = build_agent(
        RedditSettings(mode="fixtures"), source=FixtureRedditSource(), provider=provider
    )
    result = await agent.run("What does Reddit say about prompt injection?")
    assert result.answer == "Found a relevant thread."
    tool_msg = provider.calls[1][-1]
    assert tool_msg.role == "tool"
    assert "2c3d4f" in tool_msg.content
    assert tool_msg.content.startswith("<untrusted_tool_output>")
    await src.aclose()


async def test_agent_offline_heuristic() -> None:
    agent, _src = build_agent(
        RedditSettings(mode="fixtures"), source=FixtureRedditSource(), provider=HeuristicProvider()
    )
    result = await agent.run("top posts in r/python")
    assert "uv workspaces" in result.answer


def test_cli_ask_and_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDDIT_MODE", "fixtures")
    monkeypatch.setenv("LLM_PROVIDER", "heuristic")
    runner = CliRunner()
    res = runner.invoke(app, ["ask", "hot posts in r/dataengineering", "-o", "json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["tools"] == ["get_subreddit_posts"]
    res2 = runner.invoke(app, ["posts", "python", "--listing", "top"])
    assert res2.exit_code == 0, res2.output
    assert "uv workspaces" in res2.stdout
    res3 = runner.invoke(app, ["ask", "hot posts in r/python"])
    assert res3.exit_code == 0
