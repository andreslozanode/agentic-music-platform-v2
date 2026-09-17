from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_core.llm import HeuristicProvider
from autonomous_agent.api.app import create_app
from autonomous_agent.api.security import hash_key
from autonomous_agent.config import Settings
from autonomous_agent.runtime import Runtime
from music_agent.config import MusicSettings
from music_agent.service import MusicIntelligenceService

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


post = _load("post_deploy_verify")
watch = _load("deploy_watch")
report = _load("gate_report")

KEY = "smoke-reader-key"


@pytest.fixture
def live_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    keys = f"smoke:reader:{hash_key(KEY)}"
    settings = Settings(
        ENVIRONMENT="ci",
        storage_root=str(tmp_path / "lake"),
        state_dir=tmp_path / "st",
        api_keys=keys,
    )

    async def factory() -> Runtime:
        return await Runtime.create(
            settings,
            provider=HeuristicProvider(),
            service=MusicIntelligenceService(MusicSettings(mode="fixtures")),
        )

    with TestClient(create_app(settings, factory), base_url="https://agent.test") as client:
        yield client


def _fetch_via(client: TestClient):  # type: ignore[no-untyped-def]
    def fetch(
        url: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> Any:
        path = url.replace("https://agent.test", "")
        resp = client.request(method, path, json=body, headers=headers or {})
        return post.HttpResult(
            resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content, 0.01
        )

    return fetch


def test_post_deploy_verify_against_real_app(live_app: TestClient, tmp_path: Path) -> None:
    out, junit = tmp_path / "post.json", tmp_path / "junit.xml"
    rc = post.main(
        [
            "--base-url",
            "https://agent.test",
            "--api-key",
            KEY,
            "--json",
            str(out),
            "--junit",
            str(junit),
            "--latency-samples",
            "3",
        ],
        fetch=_fetch_via(live_app),
    )
    data = json.loads(out.read_text())
    failed = [c for c in data["checks"] if not c["passed"]]
    assert rc == 0, failed
    names = {c["name"] for c in data["checks"]}
    assert {"prompt_injection_blocked", "auth_required", "security_headers"} <= names
    assert "<testsuite" in junit.read_text()


def test_post_deploy_detects_exposed_docs_and_missing_auth(tmp_path: Path) -> None:
    def insecure(
        url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: float = 15
    ) -> Any:
        return post.HttpResult(200, {"server": "uvicorn"}, b'{"status": "answered"}', 2.0)

    rc = post.main(
        [
            "--base-url",
            "https://x.test",
            "--expect-docs-disabled",
            "--json",
            str(tmp_path / "p.json"),
            "--junit",
            str(tmp_path / "j.xml"),
            "--latency-samples",
            "2",
        ],
        fetch=insecure,
    )
    assert rc == 1
    failed = {
        c["name"]
        for c in json.loads((tmp_path / "p.json").read_text())["checks"]
        if not c["passed"]
    }
    assert {
        "security_headers",
        "auth_required",
        "api_docs_disabled",
        "latency_p95",
        "no_server_banner",
    } <= failed
    assert post.p95([]) == float("inf")
    with pytest.raises(ValueError, match="http"):
        post.request("file:///etc/passwd")


class FakeRunner:
    def __init__(self, rollout_rc: int, restarts: Sequence[int]) -> None:
        self.rollout_rc = rollout_rc
        self.restarts = list(restarts)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:3] == ["kubectl", "rollout", "status"]:
            return subprocess.CompletedProcess(cmd, self.rollout_rc, "rolled out", "")
        if cmd[:3] == ["kubectl", "get", "pods"]:
            n = self.restarts.pop(0) if self.restarts else 0
            pods = {"items": [{"status": {"containerStatuses": [{"restartCount": n}]}}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(pods), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _args(**kw: Any) -> Any:
    base = [
        "--release",
        "agentic",
        "--namespace",
        "ns",
        "--deployment",
        "api",
        "--health-url",
        "https://x/readyz",
        "--duration",
        "10",
        "--interval",
        "1",
        "--rollback",
    ]
    return watch.parse(base + [f"--{k.replace('_', '-')}={v}" for k, v in kw.items()])


def test_deploy_watch_healthy() -> None:
    runner = FakeRunner(0, [1, 1])
    res = watch.watch(_args(), runner, lambda url, t: True, lambda s: None)
    assert res.decision == "GO"
    assert res.samples == 10
    assert not any(c[0] == "helm" for c in runner.calls)


def test_deploy_watch_rolls_back_on_errors_and_restarts() -> None:
    flaky = iter([True, False] * 10)
    runner = FakeRunner(0, [0, 2])
    res = watch.watch(_args(), runner, lambda url, t: next(flaky), lambda s: None)
    assert res.decision == "NO-GO"
    assert res.failure_ratio == 0.5
    assert res.restarts == 2
    assert res.rolled_back
    assert runner.calls[-1][:4] == ["helm", "rollback", "agentic", "0"]


def test_deploy_watch_failed_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner(1, [])
    res = watch.watch(_args(), runner, lambda url, t: True, lambda s: None)
    assert not res.rollout_ok
    assert res.decision == "NO-GO"
    monkeypatch.setattr(watch, "watch", lambda a: res)
    rc = watch.main(
        [
            "--release",
            "r",
            "--namespace",
            "n",
            "--deployment",
            "d",
            "--health-url",
            "https://x",
            "--json",
            str(tmp_path / "w.json"),
        ]
    )
    assert rc == 3
    with pytest.raises(ValueError, match="http"):
        watch.http_ok("ftp://x", 1)
    assert watch.http_ok("https://127.0.0.1:9/nothing", 0.2) is False


def test_gate_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pre-deploy.jsonl").write_text(
        json.dumps(
            {
                "gate": "pre",
                "stage": "lint",
                "severity": "critical",
                "status": "passed",
                "seconds": 1,
                "detail": "",
            }
        )
        + "\n"
        + json.dumps(
            {
                "gate": "pre",
                "stage": "secrets",
                "severity": "critical",
                "status": "failed",
                "seconds": 2,
                "detail": "leak",
            }
        )
        + "\n"
    )
    (tmp_path / "deploy-watch.json").write_text(
        json.dumps(
            {
                "decision": "GO",
                "rollout_ok": True,
                "samples": 5,
                "failure_ratio": 0.0,
                "restarts": 0,
                "rolled_back": False,
            }
        )
    )
    (tmp_path / "post-deploy.json").write_text(
        json.dumps(
            {
                "decision": "GO",
                "checks": [
                    {"name": "liveness", "severity": "critical", "passed": True, "detail": "ok"}
                ],
            }
        )
    )
    (tmp_path / "eval.json").write_text(json.dumps({"passed": True, "metrics": {"x": 1}}))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = report.main([str(tmp_path), "--title", "v1", "--output", str(tmp_path / "gate.md")])
    assert rc == 1
    text = summary.read_text()
    assert "NO-GO" in text
    assert "'secrets' failed" in text
    (tmp_path / "pre-deploy.jsonl").write_text("")
    assert report.main([str(tmp_path)]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert report.main([str(empty)]) == 1
