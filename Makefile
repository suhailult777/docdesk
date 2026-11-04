# DocDesk. Run `make help` for the targets.
SHELL := /bin/bash
PY ?= .venv/bin/python
STACK_DIR ?= .switch-stack
COMPOSE := compose --profile collab --profile gateway
DOCKER ?= docker
export DOCDESK_ENV_FILE ?= $(CURDIR)/.env

.PHONY: help install stack up down status regkey setup run demo test scan verify-image verify run-fixer

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## create .venv and install DocDesk with test deps
	uv venv -q .venv 2>/dev/null || python3 -m venv .venv
	uv pip install -q --python $(PY) -e '.[dev]' 2>/dev/null || $(PY) -m pip install -q -e '.[dev]'

stack: ## download the pinned Switch 0.31.0 stack into $(STACK_DIR)
	scripts/switch_stack.sh $(STACK_DIR)

up: ## start Switch 0.31.0 (Postgres, switch-core, Gateway, Mattermost)
	@[ -f $(STACK_DIR)/docker-compose.yml ] || scripts/switch_stack.sh $(STACK_DIR)
	cd $(STACK_DIR) && $(DOCKER) $(COMPOSE) up -d
	@for i in $$(seq 1 60); do curl -fs localhost:8000/health >/dev/null && break; sleep 2; done
	@curl -fs localhost:8000/health && echo "  Switch is up: Mattermost http://localhost:8065, Gateway http://localhost:3000"

down: ## stop the stack, keep its data
	cd $(STACK_DIR) && $(DOCKER) $(COMPOSE) down

status: ## show the stack containers
	cd $(STACK_DIR) && $(DOCKER) $(COMPOSE) ps

regkey: ## create a registration key in the Gateway and save it to the env file
	SWITCH_STACK_ENV=$(STACK_DIR)/.env $(PY) scripts/registration_key.py

setup: ## register docdesk and create #acme-help and #acme-docs-maint from the template
	$(PY) scripts/setup_switch.py

run: ## run the DocDesk agent (foreground)
	$(PY) -m agent.main

run-fixer: ## run the doc-fixer agent (foreground); needs gh logged in and the verify image
	DOCDESK_DOCKER="$(DOCKER)" $(PY) -m agent.fixer

demo: ## ask the seed questions in #acme-help as the teammate and print the replies
	@while IFS= read -r q; do [ -z "$$q" ] && continue; $(PY) scripts/ask.py "@docdesk $$q" || exit 1; echo; done < demo/seed_questions.txt

test: ## run the unit tests (no network, no keys)
	$(PY) -m pytest -q

scan: ## look for secrets in tracked files before a push
	@! git grep -nIE "(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}" -- . ':!*.example' \
	  || (echo "possible secret above, not pushing" && exit 1)
	@echo "secret scan clean"

verify-image: ## build the sandbox image used to run doc commands (docker run --network none)
	$(DOCKER) build -q -t docdesk-verify:node20 verify/

verify: ## run the commands in FILES of the demo repo in the sandbox, e.g. make verify FILES=README.md
	DOCDESK_DOCKER="$(DOCKER)" $(PY) -m agent.verify $${DOCDESK_REPO_DIR:-demo/acme-shop} $(FILES)
