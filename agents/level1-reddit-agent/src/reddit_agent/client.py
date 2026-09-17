"""Read-only Reddit data access.

Reddit closed self-service Data API access in November 2025 (Responsible Builder
Policy): credentials must be approved by Reddit before use. This client therefore
supports two sources behind one protocol:

* :class:`RedditOAuthClient` - application-only OAuth against ``oauth.reddit.com``
  using approved credentials; honours the 100 QPM free-tier limit.
* :class:`FixtureRedditSource` - bundled, synthetic sample data for offline demos,
  CI and e2e tests. Never used in production (enforced by config validation).

Unauthenticated ``*.json`` scraping is intentionally not implemented.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from agent_core.http import RateLimiter, build_http_client, request_json
from reddit_agent.config import RedditSettings

Listing = Literal["hot", "new", "top", "rising"]
TimeFilter = Literal["hour", "day", "week", "month", "year", "all"]

SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
POST_ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
MAX_SELFTEXT = 1_500


class RedditPost(BaseModel):
    id: str
    subreddit: str
    title: str
    author: str | None = None
    score: int = 0
    num_comments: int = 0
    created_utc: datetime
    url: str | None = None
    permalink: str
    selftext: str = Field(default="", max_length=MAX_SELFTEXT + 20)
    over_18: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RedditPost:
        text = str(data.get("selftext") or "")
        if len(text) > MAX_SELFTEXT:
            text = text[:MAX_SELFTEXT] + "…"
        return cls(
            id=str(data["id"]),
            subreddit=str(data.get("subreddit", "")),
            title=str(data.get("title", "")),
            author=data.get("author"),
            score=int(data.get("score", 0)),
            num_comments=int(data.get("num_comments", 0)),
            created_utc=datetime.fromtimestamp(float(data.get("created_utc", 0)), tz=UTC),
            url=data.get("url"),
            permalink=f"https://www.reddit.com{data.get('permalink', '')}",
            selftext=text,
            over_18=bool(data.get("over_18", False)),
        )


class RedditComment(BaseModel):
    id: str
    author: str | None = None
    body: str
    score: int = 0
    depth: int = 0


class SubredditInfo(BaseModel):
    name: str
    title: str
    subscribers: int | None = None
    public_description: str = ""
    over18: bool = False


def validate_subreddit(name: str) -> str:
    name = name.removeprefix("r/").strip()
    if not SUBREDDIT_RE.fullmatch(name):
        raise ValueError(f"invalid subreddit name: {name!r}")
    return name


def validate_post_id(post_id: str) -> str:
    post_id = post_id.removeprefix("t3_").strip().lower()
    if not POST_ID_RE.fullmatch(post_id):
        raise ValueError(f"invalid post id: {post_id!r}")
    return post_id


def _posts(listing: dict[str, Any]) -> list[RedditPost]:
    children = listing.get("data", {}).get("children", [])
    return [RedditPost.from_api(c["data"]) for c in children if c.get("kind") == "t3"]


def _flatten_comments(nodes: list[dict[str, Any]], depth: int, out: list[RedditComment]) -> None:
    for node in nodes:
        if node.get("kind") != "t1":
            continue
        data = node["data"]
        out.append(
            RedditComment(
                id=str(data["id"]),
                author=data.get("author"),
                body=str(data.get("body", ""))[:MAX_SELFTEXT],
                score=int(data.get("score", 0)),
                depth=depth,
            )
        )
        replies = data.get("replies")
        if isinstance(replies, dict):
            _flatten_comments(replies.get("data", {}).get("children", []), depth + 1, out)


class RedditSource(Protocol):
    async def subreddit_posts(
        self, subreddit: str, listing: Listing, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]: ...

    async def search(
        self, query: str, subreddit: str | None, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]: ...

    async def comments(self, subreddit: str, post_id: str, limit: int) -> list[RedditComment]: ...

    async def subreddit_info(self, subreddit: str) -> SubredditInfo: ...

    async def aclose(self) -> None: ...


class RedditOAuthClient:
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - URL
    API = "https://oauth.reddit.com"

    def __init__(
        self, settings: RedditSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not (settings.client_id and settings.client_secret):
            raise ValueError("REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are required in api mode")
        self._settings = settings
        self._http = build_http_client(
            allowed_hosts=["www.reddit.com", "oauth.reddit.com"],
            user_agent=settings.user_agent,
            transport=transport,
        )
        self._limiter = RateLimiter(rate=settings.requests_per_minute, per=60.0)
        self._token: str | None = None
        self._token_expiry = 0.0

    async def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_expiry:
            secret = self._settings.client_secret
            if secret is None:  # pragma: no cover - guarded in __init__
                raise RuntimeError("client secret missing")
            raw = f"{self._settings.client_id}:{secret.get_secret_value()}"
            basic = base64.b64encode(raw.encode()).decode()
            data = await request_json(
                self._http,
                "POST",
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
                limiter=self._limiter,
            )
            self._token = str(data["access_token"])
            self._token_expiry = time.monotonic() + float(data.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await request_json(
            self._http,
            "GET",
            f"{self.API}{path}",
            params={**params, "raw_json": 1},
            headers=await self._auth_header(),
            limiter=self._limiter,
        )

    async def subreddit_posts(
        self, subreddit: str, listing: Listing, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]:
        sub = validate_subreddit(subreddit)
        data = await self._get(f"/r/{sub}/{listing}", {"limit": limit, "t": time_filter})
        return _posts(data)

    async def search(
        self, query: str, subreddit: str | None, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]:
        params: dict[str, Any] = {"q": query, "limit": limit, "t": time_filter, "sort": "relevance"}
        if subreddit:
            path = f"/r/{validate_subreddit(subreddit)}/search"
            params["restrict_sr"] = 1
        else:
            path = "/search"
        return _posts(await self._get(path, params))

    async def comments(self, subreddit: str, post_id: str, limit: int) -> list[RedditComment]:
        sub, pid = validate_subreddit(subreddit), validate_post_id(post_id)
        data = await self._get(f"/r/{sub}/comments/{pid}", {"limit": limit, "depth": 2})
        out: list[RedditComment] = []
        if isinstance(data, list) and len(data) > 1:
            _flatten_comments(data[1].get("data", {}).get("children", []), 0, out)
        return out[:limit]

    async def subreddit_info(self, subreddit: str) -> SubredditInfo:
        data = (await self._get(f"/r/{validate_subreddit(subreddit)}/about", {}))["data"]
        return SubredditInfo(
            name=str(data.get("display_name", subreddit)),
            title=str(data.get("title", "")),
            subscribers=data.get("subscribers"),
            public_description=str(data.get("public_description", ""))[:MAX_SELFTEXT],
            over18=bool(data.get("over18", False)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class FixtureRedditSource:
    """Synthetic sample data shipped with the package (offline/CI only)."""

    def __init__(self) -> None:
        raw = resources.files("reddit_agent.fixtures").joinpath("sample.json").read_text("utf-8")
        self._data: dict[str, Any] = json.loads(raw)

    def _all(self) -> list[RedditPost]:
        return [RedditPost.from_api(p) for p in self._data["posts"]]

    async def subreddit_posts(
        self, subreddit: str, listing: Listing, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]:
        sub = validate_subreddit(subreddit).lower()
        posts = [p for p in self._all() if p.subreddit.lower() == sub]
        key = (lambda p: p.created_utc) if listing == "new" else (lambda p: p.score)
        return sorted(posts, key=key, reverse=True)[:limit]

    async def search(
        self, query: str, subreddit: str | None, time_filter: TimeFilter, limit: int
    ) -> list[RedditPost]:
        terms = {t for t in query.lower().split() if len(t) > 2}
        posts = self._all()
        if subreddit:
            sub = validate_subreddit(subreddit).lower()
            posts = [p for p in posts if p.subreddit.lower() == sub]
        scored = [(sum(t in f"{p.title} {p.selftext}".lower() for t in terms), p) for p in posts]
        return [p for s, p in sorted(scored, key=lambda x: -x[0]) if s > 0][:limit]

    async def comments(self, subreddit: str, post_id: str, limit: int) -> list[RedditComment]:
        pid = validate_post_id(post_id)
        return [RedditComment(**c) for c in self._data["comments"].get(pid, [])][:limit]

    async def subreddit_info(self, subreddit: str) -> SubredditInfo:
        sub = validate_subreddit(subreddit).lower()
        for info in self._data["subreddits"]:
            if info["name"].lower() == sub:
                return SubredditInfo(**info)
        raise LookupError(f"subreddit {sub} not in fixtures")

    async def aclose(self) -> None:
        return None


def build_source(settings: RedditSettings) -> RedditSource:
    if settings.mode == "fixtures":
        return FixtureRedditSource()
    return RedditOAuthClient(settings)
