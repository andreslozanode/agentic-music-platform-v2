"""``autonomous-agent`` command line interface."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import typer
import uvicorn
from prometheus_client import start_http_server
from rich.console import Console
from rich.markdown import Markdown

from agent_core.observability import configure_logging
from autonomous_agent.api.app import create_app
from autonomous_agent.autonomy import AutonomousCycle
from autonomous_agent.config import Settings
from autonomous_agent.evaluation.runner import run_evaluation
from autonomous_agent.observability import configure_tracing
from autonomous_agent.responsible_ai.model_card import render_model_card
from autonomous_agent.runtime import Runtime

app = typer.Typer(help="Level 3 - governed autonomous agent.", no_args_is_help=True)
console = Console()


@app.command()
def serve(
    # Containers bind all interfaces; exposure is controlled by the Kubernetes Service,
    # Ingress and the default-deny NetworkPolicy.
    host: str = typer.Option("0.0.0.0", help="Bind address"),  # noqa: S104 # nosec B104
    port: int = typer.Option(8080),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Run the HTTP API (metrics are exposed on a separate internal port)."""
    configure_logging(log_level)
    settings = Settings()
    configure_tracing("autonomous-agent", settings.otel_endpoint)
    start_http_server(settings.metrics_port)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=20,
    )


@app.command("run-cycle")
def run_cycle(
    loop: bool = typer.Option(False, help="Keep running every AGENT_CYCLE_INTERVAL_S"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Execute the autonomous cycle once (CronJob) or continuously."""
    configure_logging(log_level)

    async def _run() -> int:
        rt = await Runtime.create()
        cycle = AutonomousCycle(rt)
        try:
            if not loop:
                report = await cycle.run_once()
                typer.echo(report.model_dump_json(indent=2))
                return 0 if report.status != "failed" else 2
            stop = asyncio.Event()
            loop_ = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop_.add_signal_handler(sig, stop.set)
            await cycle.run_forever(rt.settings.cycle_interval_s, stop)
            return 0
        finally:
            await rt.close()

    raise typer.Exit(asyncio.run(_run()))


@app.command()
def ask(question: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Ask the governed agent (runs a cycle first if the knowledge base is empty)."""
    configure_logging("WARNING")

    async def _run() -> None:
        rt = await Runtime.create()
        try:
            if await rt.store.count() == 0:
                await AutonomousCycle(rt).run_once(actor="cli-bootstrap")
            answer = await rt.agent.ask(question, principal="cli")
        finally:
            await rt.close()
        if as_json:
            typer.echo(answer.model_dump_json(indent=2))
        else:
            console.print(Markdown(answer.answer))
            console.print(
                f"[dim]status={answer.status} tools={answer.tools_used} flags={answer.flags}[/dim]"
            )

    asyncio.run(_run())


@app.command()
def evaluate(
    output: Path = typer.Option(Path("reports/eval.json")),
    bootstrap: bool = typer.Option(True, help="Run a cycle to populate the knowledge base"),
) -> None:
    """Run golden + red-team evaluation; exits non-zero when thresholds are not met."""
    configure_logging("WARNING")

    async def _run() -> bool:
        rt = await Runtime.create()
        try:
            if bootstrap:
                await AutonomousCycle(rt).run_once(actor="evaluation-bootstrap")
            report = await run_evaluation(rt, output)
        finally:
            await rt.close()
        console.print_json(
            json.dumps(
                {"passed": report.passed, "metrics": report.metrics, "failures": report.failures}
            )
        )
        return report.passed

    if not asyncio.run(_run()):
        raise typer.Exit(1)


@app.command("verify-audit")
def verify_audit() -> None:
    """Verify the HMAC hash chain of this pod's audit log."""

    async def _run() -> bool:
        rt = await Runtime.create()
        try:
            result = rt.audit.verify()
        finally:
            await rt.close()
        typer.echo(result.model_dump_json())
        return result.valid

    if not asyncio.run(_run()):
        raise typer.Exit(1)


@app.command("model-card")
def model_card(eval_report: Path = typer.Option(Path("reports/eval.json"))) -> None:
    """Print the AI system card for the current configuration."""

    async def _run() -> str:
        rt = await Runtime.create()
        try:
            return render_model_card(rt, eval_report)
        finally:
            await rt.close()

    typer.echo(asyncio.run(_run()))


if __name__ == "__main__":  # pragma: no cover
    app()
