#!/usr/bin/env bash
# Pre-deploy quality & security gate. Writes one JSON object per stage to
# $REPORT_DIR/pre-deploy.jsonl and exits non-zero if any critical stage fails.
#
#   STRICT=1          missing tools fail the gate (use in CI)
#   REPORT_DIR=...    evidence directory (default: reports)
#   SKIP_STAGES=a,b   comma-separated stage names to skip (logged, never silent)
set -Eeuo pipefail

REPORT_DIR="${REPORT_DIR:-reports}"
STRICT="${STRICT:-0}"
SKIP_STAGES="${SKIP_STAGES:-}"
COVERAGE_MIN="${COVERAGE_MIN:-80}"
CHART_DIR="${CHART_DIR:-deploy/helm/agentic-platform}"
POLICY_APP="${POLICY_APP:-agents/level3-autonomous-agent/src/autonomous_agent/governance/policies/default.yaml}"
POLICY_CHART="${POLICY_CHART:-${CHART_DIR}/files/policy.yaml}"
RUN="${RUN:-uv run --frozen}"

mkdir -p "$REPORT_DIR"
OUT="$REPORT_DIR/pre-deploy.jsonl"
: > "$OUT"
FAILED=0

json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[-4000:]))'; }

record() { # name severity status seconds log
  local detail
  detail="$(printf '%s' "$5" | json_escape)"
  printf '{"gate":"pre","stage":"%s","severity":"%s","status":"%s","seconds":%s,"detail":%s}\n' \
    "$1" "$2" "$3" "$4" "$detail" >> "$OUT"
}

have() { command -v "$1" >/dev/null 2>&1; }

stage() { # name severity required_tool -- command...
  local name="$1" severity="$2" tool="$3"; shift 4
  if [[ ",${SKIP_STAGES}," == *",${name},"* ]]; then
    echo "::warning::stage '${name}' skipped by SKIP_STAGES"
    record "$name" "$severity" "skipped" 0 "skipped by SKIP_STAGES"
    return 0
  fi
  if [[ -n "$tool" ]] && ! have "$tool"; then
    if [[ "$STRICT" == "1" && "$severity" == "critical" ]]; then
      echo "::error::required tool '${tool}' missing for stage '${name}'"
      record "$name" "$severity" "failed" 0 "tool ${tool} not installed (STRICT=1)"
      FAILED=1
    else
      echo "::warning::tool '${tool}' missing, stage '${name}' not run"
      record "$name" "$severity" "skipped" 0 "tool ${tool} not installed"
    fi
    return 0
  fi
  echo "::group::[pre-deploy] ${name}"
  local start log rc
  start=$(date +%s)
  set +e
  log="$("$@" 2>&1)"
  rc=$?
  set -e
  echo "$log" | tail -n 60
  echo "::endgroup::"
  local secs=$(( $(date +%s) - start ))
  if [[ $rc -eq 0 ]]; then
    record "$name" "$severity" "passed" "$secs" "$(echo "$log" | tail -n 5)"
  else
    record "$name" "$severity" "failed" "$secs" "$log"
    if [[ "$severity" == "critical" ]]; then FAILED=1; fi
    echo "::error::stage '${name}' failed (rc=${rc})"
  fi
}

audit_deps() {
  local req
  req="$(mktemp)"
  uv export --frozen --all-packages --no-emit-workspace --no-hashes --format requirements-txt > "$req"
  $RUN pip-audit --strict --progress-spinner off -r "$req"
}

CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
FAKE_DIGEST="sha256:$(printf '0%.0s' $(seq 1 64))"

render_chart() {
  helm lint "$CHART_DIR" --strict --values "$CHART_DIR/values-ci.yaml"
  local env
  for env in ci staging prod; do
    # prod refuses tag-only images, so render with a placeholder digest
    helm template agentic "$CHART_DIR" --namespace "agentic-${env}" \
      --values "$CHART_DIR/values-${env}.yaml" --set image.digest="$FAKE_DIGEST" \
      > "$REPORT_DIR/rendered-${env}.yaml"
    if have kubeconform; then
      kubeconform -strict -summary -schema-location default -schema-location "$CRD_SCHEMAS" \
        "$REPORT_DIR/rendered-${env}.yaml"
    fi
  done
  if have kubeconform; then
    kubeconform -strict -summary -schema-location default -schema-location "$CRD_SCHEMAS" \
      deploy/k8s/
  fi
}

policy_drift() { cmp -s "$POLICY_APP" "$POLICY_CHART" || { echo "policy drift: $POLICY_APP != $POLICY_CHART"; return 1; }; }

stage lint          critical uv       -- $RUN ruff check . --output-format=github
stage format        critical uv       -- $RUN ruff format --check .
stage types         critical uv       -- $RUN mypy packages/agent-core/src agents/level1-reddit-agent/src agents/level2-music-agent/src agents/level3-autonomous-agent/src
stage tests         critical uv       -- $RUN pytest -q --cov --cov-report=xml:"$REPORT_DIR/coverage.xml" --cov-fail-under="$COVERAGE_MIN" --junitxml="$REPORT_DIR/junit.xml"
stage sast          critical uv       -- $RUN bandit -q -ll -c pyproject.toml -r packages agents
stage dependencies  critical uv       -- audit_deps
stage secrets       critical gitleaks -- gitleaks dir --redact --no-banner --exit-code 1 --config .gitleaks.toml .
stage iac-helm      critical helm     -- render_chart
stage iac-checkov   critical checkov  -- checkov --compact --framework kubernetes -f "$REPORT_DIR/rendered-prod.yaml" -f "$REPORT_DIR/rendered-staging.yaml"
stage policy-drift  critical cmp      -- policy_drift
stage llm-eval      critical uv       -- $RUN autonomous-agent evaluate --output "$REPORT_DIR/eval.json"

if [[ $FAILED -ne 0 ]]; then
  echo "PRE-DEPLOY GATE: NO-GO (see $OUT)"
  exit 1
fi
echo "PRE-DEPLOY GATE: GO"
