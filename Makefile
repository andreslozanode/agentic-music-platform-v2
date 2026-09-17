# Developer entry points. Everything runs offline unless stated otherwise.
SHELL := /bin/bash
.DEFAULT_GOAL := help
UV ?= uv run --frozen
SRC := packages/agent-core/src agents/level1-reddit-agent/src agents/level2-music-agent/src agents/level3-autonomous-agent/src
CHART := deploy/helm/agentic-platform
POLICY_APP := agents/level3-autonomous-agent/src/autonomous_agent/governance/policies/default.yaml
OFFLINE := ENVIRONMENT=ci LLM_PROVIDER=heuristic MUSIC_MODE=fixtures

.PHONY: help setup fmt lint types test cov eval gates policy-check helm-lint docker-build \
        api serve cycle ask api-key e2e-local clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install the workspace (Python 3.12)
	uv sync --all-packages

fmt: ## Format the code
	$(UV) ruff format .
	$(UV) ruff check . --fix

lint: ## Lint and check formatting
	$(UV) ruff check .
	$(UV) ruff format --check .

types: ## Strict type check
	$(UV) mypy $(SRC)

test: ## Run the test suite
	$(UV) pytest -q

cov: ## Tests with coverage (fails under 80%)
	$(UV) pytest -q --cov --cov-report=term-missing

eval: ## Golden + red-team evaluation gate
	$(OFFLINE) $(UV) autonomous-agent evaluate --output reports/eval.json

policy-check: ## Fail if the packaged policy and the chart copy drift
	@cmp -s $(POLICY_APP) $(CHART)/files/policy.yaml \
		&& echo "policy in sync" \
		|| { echo "policy drift: $(POLICY_APP) != $(CHART)/files/policy.yaml"; exit 1; }

helm-lint: ## Lint and render the chart for every environment
	helm lint $(CHART) --strict -f $(CHART)/values-ci.yaml
	@for env in ci staging prod; do \
		helm template agentic $(CHART) -n agentic-$$env -f $(CHART)/values-$$env.yaml \
			--set image.digest=sha256:$$(printf '0%.0s' {1..64}) > /dev/null && echo "rendered $$env"; \
	done

gates: ## Full pre-deploy gate (the deployment skill)
	$(OFFLINE) STRICT=0 skills/deploy-quality-gates/scripts/pre_deploy_gate.sh

docker-build: ## Build the three images
	docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=autonomous-agent -t agentic-api:dev .
	docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=music-agent -t music-mcp:dev .
	docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=reddit-agent -t reddit-agent:dev .

serve: ## Run the governed API locally (offline)
	$(OFFLINE) $(UV) autonomous-agent serve

cycle: ## Run one autonomous cycle (offline)
	$(OFFLINE) $(UV) autonomous-agent run-cycle

ask: ## Ask the agent: make ask Q="which new artists are emerging?"
	$(OFFLINE) $(UV) autonomous-agent ask "$(Q)"

api-key: ## Generate an API key: make api-key NAME=alice ROLES="reader|approver"
	@NAME=$${NAME:-dev}; ROLES=$${ROLES:-reader}; \
	KEY=$$(python3 -c "import secrets;print(secrets.token_urlsafe(32))"); \
	DIGEST=$$(printf '%s' "$$KEY" | sha256sum | cut -d' ' -f1); \
	echo "secret key (store it in your password manager, it is never recoverable):"; \
	echo "  $$KEY"; \
	echo "AGENT_API_KEYS entry:"; \
	echo "  $$NAME:$$ROLES:$$DIGEST"

e2e-local: ## kind cluster + helm install + post-deploy gate
	kind create cluster --config deploy/kind/kind-config.yaml || true
	kubectl apply -f deploy/kind/namespace-ci.yaml
	docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=autonomous-agent -t agentic-api:ci .
	kind load docker-image agentic-api:ci --name agentic-e2e
	helm upgrade --install agentic $(CHART) -n agentic-ci -f $(CHART)/values-ci.yaml --wait --atomic
	helm test agentic -n agentic-ci --logs

clean: ## Remove local state and reports
	rm -rf var reports .pytest_cache .ruff_cache .mypy_cache
