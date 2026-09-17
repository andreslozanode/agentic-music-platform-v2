"""``music-agent`` command line interface."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from agent_core.observability import configure_logging
from music_agent.agent import build_agent
from music_agent.service import MusicIntelligenceService

app = typer.Typer(help="Level 2 - music intelligence agent.", no_args_is_help=True)
console = Console()


@app.command()
def ask(
    question: str,
    output: Literal["text", "json"] = typer.Option("text", "--output", "-o"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """Ask the music agent a question."""
    configure_logging(log_level)

    async def _run() -> None:
        agent, service = build_agent()
        try:
            result = await agent.run(question)
        finally:
            await service.aclose()
        if output == "json":
            typer.echo(
                json.dumps(
                    {
                        "answer": result.answer,
                        "tools": [c.name for s in result.steps for c in s.tool_calls],
                        "usage": result.usage.model_dump(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            console.print(Markdown(result.answer or "_(no answer)_"))

    asyncio.run(_run())


@app.command()
def snapshot(as_json: bool = typer.Option(False, "--json")) -> None:
    """Fetch latest releases, new artists, playlists and the global Top 50 (no LLM)."""
    configure_logging("WARNING")

    async def _run() -> None:
        service = MusicIntelligenceService()
        try:
            snap = await service.snapshot()
        finally:
            await service.aclose()
        if as_json:
            typer.echo(snap.model_dump_json(indent=2))
            return
        if snap.top_global:
            table = Table(title=snap.top_global.label)
            for col in ("#", "track", "artist", "listens"):
                table.add_column(col)
            for t in snap.top_global.tracks[:50]:
                table.add_row(str(t.rank), t.title, t.artist, str(t.listen_count or ""))
            console.print(table)
        console.print(f"latest releases: {len(snap.latest_releases)}")
        console.print(f"new artists: {', '.join(a.name for a in snap.new_artists[:10])}")
        console.print(f"playlists: {', '.join(p.title for p in snap.playlists[:10])}")
        for name, err in snap.errors.items():
            console.print(f"[red]{name} failed:[/red] {err}")

    asyncio.run(_run())


@app.command()
def mcp(
    transport: Literal["stdio", "sse", "streamable-http"] = typer.Option("stdio"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    allowed_host: list[str] = typer.Option(
        [], help="Host header values accepted (DNS-rebinding protection)"
    ),
) -> None:  # pragma: no cover - blocking server
    """Serve the music tools over the Model Context Protocol."""
    from music_agent.mcp_server import run

    run(transport, host=host, port=port, allowed_hosts=allowed_host or None)


if __name__ == "__main__":  # pragma: no cover
    app()
