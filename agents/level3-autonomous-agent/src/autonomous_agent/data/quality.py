"""Declarative data-quality checks. Critical failures stop promotion to the next layer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pyarrow as pa
import pyarrow.compute as pc

Severity = Literal["critical", "warning"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class QualityReport:
    table: str
    rows: int
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results if r.severity == "critical")

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]


class DataQualityError(RuntimeError):
    def __init__(self, report: QualityReport) -> None:
        names = ", ".join(r.name for r in report.failures if r.severity == "critical")
        super().__init__(f"critical data-quality failures in {report.table}: {names}")
        self.report = report


Check = Callable[[pa.Table], CheckResult]


def not_null(*cols: str, severity: Severity = "critical") -> Check:
    def run(t: pa.Table) -> CheckResult:
        bad = {c: t[c].null_count for c in cols if t[c].null_count}
        return CheckResult(f"not_null({','.join(cols)})", severity, not bad, str(bad or ""))

    return run


def unique(*cols: str, severity: Severity = "critical") -> Check:
    def run(t: pa.Table) -> CheckResult:
        keys = list(zip(*(t[c].to_pylist() for c in cols), strict=True))
        dupes = len(keys) - len(set(keys))
        return CheckResult(f"unique({','.join(cols)})", severity, dupes == 0, f"dupes={dupes}")

    return run


def in_range(col: str, lo: float, hi: float, severity: Severity = "critical") -> Check:
    def run(t: pa.Table) -> CheckResult:
        values = t[col].drop_null()
        if len(values) == 0:
            return CheckResult(f"range({col})", severity, True)
        mn, mx = pc.min(values).as_py(), pc.max(values).as_py()
        ok = lo <= mn and mx <= hi
        return CheckResult(f"range({col})", severity, ok, f"min={mn} max={mx}")

    return run


def min_rows(n: int, severity: Severity = "critical") -> Check:
    def run(t: pa.Table) -> CheckResult:
        return CheckResult(f"min_rows({n})", severity, t.num_rows >= n, f"rows={t.num_rows}")

    return run


def fresh(col: str, max_age: timedelta, severity: Severity = "warning") -> Check:
    def run(t: pa.Table) -> CheckResult:
        values = t[col].drop_null()
        if len(values) == 0:
            return CheckResult(f"freshness({col})", severity, False, "no values")
        newest = pc.max(values).as_py()
        if not isinstance(newest, datetime):
            newest = datetime.combine(newest, datetime.min.time(), tzinfo=UTC)
        age = datetime.now(UTC) - newest
        return CheckResult(f"freshness({col})", severity, age <= max_age, f"age={age}")

    return run


def accepted_values(col: str, allowed: set[str], severity: Severity = "critical") -> Check:
    def run(t: pa.Table) -> CheckResult:
        bad = {v for v in t[col].drop_null().to_pylist() if v not in allowed}
        return CheckResult(f"accepted_values({col})", severity, not bad, str(sorted(bad)))

    return run


def schema_matches(expected: pa.Schema) -> Check:
    def run(t: pa.Table) -> CheckResult:
        missing = [f.name for f in expected if f.name not in t.schema.names]
        return CheckResult("schema_contract", "critical", not missing, f"missing={missing}")

    return run


def run_checks(table_name: str, table: pa.Table, checks: list[Check]) -> QualityReport:
    return QualityReport(table_name, table.num_rows, tuple(c(table) for c in checks))
