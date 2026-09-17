"""``reddit-agent`` command line interface."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from agent_core.observability import configure_logging
from reddit_agent.agent import OutputFormat, build_agent
from reddit_agent.client import Listing, TimeFilter, build_source
from reddit_agent.config import RedditSettings

app = typer.Typer(
    help="Level 1 - ad-hoc Reddit queries powered by an AI agent.", no_args_is_help=True
)
console = Console()


@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural-language question"),
    output: OutputFormat = typer.Option("text", "--output", "-o"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """Ask the agent a question about Reddit."""
    configure_logging(log_level)

    async def _run() -> None:
        agent, source = build_agent()
        try:
            result = await agent.run(question)
        finally:
            await source.aclose()
        if output == "json":
            payload = {
                "answer": result.answer,
                "tools": [c.name for s in result.steps for c in s.tool_calls],
                "usage": result.usage.model_dump(),
                "stopped_reason": result.stopped_reason,
            }
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(Markdown(result.answer or "_(no answer)_"))

    asyncio.run(_run())


@app.command()
def posts(
    subreddit: str,
    listing: Listing = typer.Option("hot"),
    time_filter: TimeFilter = typer.Option("week", "--time"),
    limit: int = typer.Option(5, min=1, max=25),
) -> None:
    """List posts directly (no LLM involved)."""
    configure_logging("WARNING")

    async def _run() -> None:
        settings = RedditSettings()
        source = build_source(settings)
        try:
            items = await source.subreddit_posts(subreddit, listing, time_filter, limit)
        finally:
            await source.aclose()
        table = Table(title=f"r/{subreddit} - {listing}")
        for col in ("score", "comments", "title", "link"):
            table.add_column(col)
        for p in items:
            if p.over_18 and not settings.include_nsfw:
                continue
            table.add_row(str(p.score), str(p.num_comments), p.title, p.permalink)
        console.print(table)

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    app()
