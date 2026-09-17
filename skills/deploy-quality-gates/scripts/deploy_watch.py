#!/usr/bin/env python3
"""During-deploy gate: watch a rollout and roll back automatically on regression.

Stdlib only. Exit codes: 0 healthy, 2 rolled back (or would roll back), 3 rollout failed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Probe = Callable[[str, float], bool]


def run_cmd(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False, timeout=900)


def http_ok(url: str, timeout: float) -> bool:
    if not url.startswith(("https://", "http://")):
        raise ValueError("health URL must be http(s)")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


@dataclass
class WatchResult:
    gate: str = "during"
    rollout_ok: bool = False
    samples: int = 0
    failures: int = 0
    failure_ratio: float = 0.0
    restarts: int = 0
    rolled_back: bool = False
    decision: str = "GO"
    events: list[str] = field(default_factory=list)


def pod_restarts(runner: Runner, namespace: str, selector: str) -> int:
    proc = runner(["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"])
    if proc.returncode != 0:
        return 0
    pods = json.loads(proc.stdout or "{}").get("items", [])
    return sum(
        int(cs.get("restartCount", 0))
        for pod in pods
        for cs in pod.get("status", {}).get("containerStatuses", []) or []
    )


def watch(
    args: argparse.Namespace,
    runner: Runner = run_cmd,
    probe: Probe = http_ok,
    sleep: Callable[[float], None] = time.sleep,
) -> WatchResult:
    res = WatchResult()
    selector = args.selector or f"app.kubernetes.io/instance={args.release}"
    status = runner(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{args.deployment}",
            "-n",
            args.namespace,
            f"--timeout={args.rollout_timeout}s",
        ]
    )
    res.rollout_ok = status.returncode == 0
    res.events.append(f"rollout status rc={status.returncode}: {status.stdout.strip()[-200:]}")
    baseline = pod_restarts(runner, args.namespace, selector)

    if res.rollout_ok:
        deadline = args.duration
        elapsed = 0.0
        while elapsed < deadline:
            res.samples += 1
            if not probe(args.health_url, args.timeout):
                res.failures += 1
            sleep(args.interval)
            elapsed += args.interval
        res.failure_ratio = round(res.failures / res.samples, 4) if res.samples else 1.0
        res.restarts = max(0, pod_restarts(runner, args.namespace, selector) - baseline)

    breached = (
        not res.rollout_ok
        or res.failure_ratio > args.max_failure_ratio
        or res.restarts > args.max_restarts
    )
    if breached:
        res.decision = "NO-GO"
        res.events.append(
            f"budget breached: ratio={res.failure_ratio} restarts={res.restarts} "
            f"rollout_ok={res.rollout_ok}"
        )
        if args.rollback:
            rb = runner(
                [
                    "helm",
                    "rollback",
                    args.release,
                    "0",
                    "-n",
                    args.namespace,
                    "--wait",
                    f"--timeout={args.rollout_timeout}s",
                ]
            )
            res.rolled_back = rb.returncode == 0
            res.events.append(f"helm rollback rc={rb.returncode}")
    return res


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--deployment", required=True)
    p.add_argument("--health-url", required=True)
    p.add_argument("--selector", default=None)
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--timeout", type=float, default=5)
    p.add_argument("--rollout-timeout", type=int, default=300)
    p.add_argument("--max-failure-ratio", type=float, default=0.05)
    p.add_argument("--max-restarts", type=int, default=0)
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--json", type=Path, default=Path("reports/deploy-watch.json"))
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    result = watch(args)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(asdict(result), indent=2), "utf-8")
    print(json.dumps(asdict(result), indent=2))
    if result.decision == "GO":
        return 0
    return 2 if result.rollout_ok else 3


if __name__ == "__main__":
    sys.exit(main())
