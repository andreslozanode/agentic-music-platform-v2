#!/usr/bin/env python3
"""Aggregate gate evidence into a GO / NO-GO markdown report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def load(report_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = {"pre": [], "during": None, "post": None, "eval": None}
    pre = report_dir / "pre-deploy.jsonl"
    if pre.exists():
        data["pre"] = [json.loads(line) for line in pre.read_text("utf-8").splitlines() if line]
    for key, name in (
        ("during", "deploy-watch.json"),
        ("post", "post-deploy.json"),
        ("eval", "eval.json"),
    ):
        path = report_dir / name
        if path.exists():
            data[key] = json.loads(path.read_text("utf-8"))
    return data


def decide(data: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = [
        f"pre-deploy stage '{s['stage']}' failed"
        for s in data["pre"]
        if s["status"] == "failed" and s["severity"] == "critical"
    ]
    if data["during"] and data["during"].get("decision") != "GO":
        reasons.append("rollout breached its error budget")
    if data["post"] and data["post"].get("decision") != "GO":
        reasons.extend(
            f"post-deploy check '{c['name']}' failed"
            for c in data["post"]["checks"]
            if not c["passed"] and c["severity"] == "critical"
        )
    if data["eval"] and not data["eval"].get("passed", False):
        reasons.append(f"LLM evaluation failed: {data['eval'].get('failures')}")
    if not any([data["pre"], data["during"], data["post"]]):
        reasons.append("no gate evidence found")
    return not reasons, reasons


def render(data: dict[str, Any], title: str) -> str:
    ok, reasons = decide(data)
    lines = [f"## Deployment gate: {'GO ✅' if ok else 'NO-GO ❌'} - {title}", ""]
    if data["pre"]:
        passed = sum(s["status"] == "passed" for s in data["pre"])
        lines += [
            f"### Pre-deploy {passed}/{len(data['pre'])}",
            "",
            "| stage | severity | status | seconds |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {s['stage']} | {s['severity']} | {s['status']} | {s['seconds']} |"
            for s in data["pre"]
        ]
        lines.append("")
    if data["during"]:
        d = data["during"]
        lines += [
            "### During deploy",
            "",
            f"- rollout ok: {d['rollout_ok']}, samples: {d['samples']}, "
            f"failure ratio: {d['failure_ratio']}, restarts: {d['restarts']}, "
            f"rolled back: {d['rolled_back']}",
            "",
        ]
    if data["post"]:
        checks = data["post"]["checks"]
        lines += [
            f"### Post-deploy {sum(c['passed'] for c in checks)}/{len(checks)}",
            "",
            "| check | severity | result | detail |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {c['name']} | {c['severity']} | {'pass' if c['passed'] else 'FAIL'} | "
            f"{str(c['detail'])[:80]} |"
            for c in checks
        ]
        lines.append("")
    if data["eval"]:
        lines += ["### LLM evaluation", ""]
        lines += [f"- {k}: {v}" for k, v in data["eval"].get("metrics", {}).items()]
        lines.append("")
    lines += ["### Decision", ""]
    lines += [f"- {r}" for r in reasons] or ["- all critical gates passed"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report_dir", type=Path)
    p.add_argument("--title", default=os.environ.get("GITHUB_REF_NAME", "release"))
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args(argv)
    data = load(args.report_dir)
    markdown = render(data, args.title)
    if args.output:
        args.output.write_text(markdown, "utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(markdown)
    print(markdown)
    return 0 if decide(data)[0] else 1


if __name__ == "__main__":
    sys.exit(main())
