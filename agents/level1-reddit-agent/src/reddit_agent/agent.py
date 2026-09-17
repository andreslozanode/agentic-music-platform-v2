"""Level 1 agent: tools + system prompt around a :class:`RedditSource`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_core import Tool, ToolCallingAgent, ToolRegistry, build_provider
from agent_core.config import LLMSettings
from agent_core.llm import LLMProvider
from reddit_agent.client import (
    Listing,
    RedditComment,
    RedditPost,
    RedditSource,
    SubredditInfo,
    TimeFilter,
    build_source,
)
from reddit_agent.config import RedditSettings

SYSTEM_PROMPT = """You are a read-only Reddit research assistant.
- Answer the user's question using the tools; do not invent posts, scores or quotes.
- Always cite posts with their permalink.
- Summarise; never reproduce long passages verbatim.
- Do not identify, profile or speculate about individual Reddit users.
- If the data is insufficient, say so plainly.
Reply in the same language as the user."""


class SubredditPostsArgs(BaseModel):
    subreddit: str = Field(description="Subreddit name without the r/ prefix", max_length=21)
    listing: Listing = "hot"
    time_filter: TimeFilter = Field(default="week", description="Only used by 'top'")
    limit: int = Field(default=5, ge=1, le=25)


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    subreddit: str | None = Field(default=None, max_length=21)
    time_filter: TimeFilter = "month"
    limit: int = Field(default=5, ge=1, le=25)


class CommentsArgs(BaseModel):
    subreddit: str = Field(max_length=21)
    post_id: str = Field(max_length=12, description="Base36 post id, e.g. 1a2b3c")
    limit: int = Field(default=10, ge=1, le=50)


class SubredditInfoArgs(BaseModel):
    subreddit: str = Field(max_length=21)


class RedditTools:
    def __init__(self, source: RedditSource, settings: RedditSettings) -> None:
        self.source = source
        self.settings = settings

    def _filter(self, posts: list[RedditPost]) -> list[RedditPost]:
        if self.settings.include_nsfw:
            return posts
        return [p for p in posts if not p.over_18]

    def _cap(self, limit: int) -> int:
        return min(limit, self.settings.max_results)

    async def subreddit_posts(self, a: SubredditPostsArgs) -> list[RedditPost]:
        posts = await self.source.subreddit_posts(
            a.subreddit, a.listing, a.time_filter, self._cap(a.limit)
        )
        return self._filter(posts)

    async def search(self, a: SearchArgs) -> list[RedditPost]:
        posts = await self.source.search(a.query, a.subreddit, a.time_filter, self._cap(a.limit))
        return self._filter(posts)

    async def comments(self, a: CommentsArgs) -> list[RedditComment]:
        return await self.source.comments(a.subreddit, a.post_id, a.limit)

    async def info(self, a: SubredditInfoArgs) -> SubredditInfo:
        return await self.source.subreddit_info(a.subreddit)

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            [
                Tool(
                    "get_subreddit_posts",
                    "List hot/new/top/rising posts of a subreddit.",
                    SubredditPostsArgs,
                    self.subreddit_posts,
                    keywords=("hot", "top", "new", "posts", "subreddit", "trending"),
                ),
                Tool(
                    "search_reddit",
                    "Full-text search of Reddit posts, optionally restricted to one subreddit.",
                    SearchArgs,
                    self.search,
                    keywords=("search", "find", "about", "buscar", "busca"),
                ),
                Tool(
                    "get_post_comments",
                    "Fetch top comments of a post (needs subreddit and post id).",
                    CommentsArgs,
                    self.comments,
                    keywords=("comments", "comentarios", "discussion"),
                ),
                Tool(
                    "get_subreddit_info",
                    "Describe a subreddit: title, description and subscriber count.",
                    SubredditInfoArgs,
                    self.info,
                    keywords=("info", "about", "subscribers", "describe"),
                ),
            ]
        )


def build_agent(
    settings: RedditSettings | None = None,
    llm_settings: LLMSettings | None = None,
    *,
    source: RedditSource | None = None,
    provider: LLMProvider | None = None,
) -> tuple[ToolCallingAgent, RedditSource]:
    settings = settings or RedditSettings()
    src = source or build_source(settings)
    tools = RedditTools(src, settings)
    agent = ToolCallingAgent(
        provider or build_provider(llm_settings),
        tools.registry(),
        SYSTEM_PROMPT,
        max_steps=4,
        max_tool_calls=6,
    )
    return agent, src


OutputFormat = Literal["text", "json"]
