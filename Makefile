# Zero-downtime blue/green deploy entry points.
#
# Designed for two callers:
#   - a CI job that runs `make deploy` after a successful image build
#   - an operator's `ssh host make -C /opt/svc deploy`
#
# Both invocations must be non-interactive, idempotent enough to retry
# without leaving the proxy mid-cutover, and explicit about exit codes:
#   0  new color is live and serving
#   2  smoke failed, previous color cleanly restored
#   *  a step failed; the operator must inspect
#
# Override knobs are plain `make VAR=value` overrides so a CI YAML file
# or an `ssh ... env VAR=value make deploy` invocation can set them
# inline without ever touching this Makefile.

SHELL := /bin/sh
.DEFAULT_GOAL := help

# Anchor every relative path to the directory holding *this* Makefile.
# That way `make -C /opt/svc deploy` and a local `make deploy` behave
# identically regardless of the caller's working directory.
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

PROJECT_NAME ?= rollout
COMPOSE_FILE ?= $(ROOT)/docker-compose.yml
COMPOSE := docker compose --project-name $(PROJECT_NAME) -f $(COMPOSE_FILE)

PYTHON ?= python3
ROLLOUT_MODULE := scripts.rollout

STATE_DIR ?= $(ROOT)/state
STATE_FILE ?= $(STATE_DIR)/active-color
UPSTREAM_CONF ?= $(ROOT)/nginx/conf.d/upstream.conf

# Smoke knobs flow straight through to scripts.rollout. Anything left
# blank here is simply omitted from the argv so the module's own
# environment-variable defaults take over.
SMOKE_URL ?=
SMOKE_STATUS ?=
SMOKE_ATTEMPTS ?=
SMOKE_TIMEOUT ?=
SMOKE_DELAY ?=

SMOKE_FLAGS :=
ifneq ($(strip $(SMOKE_URL)),)
SMOKE_FLAGS += --smoke-url $(SMOKE_URL)
endif
ifneq ($(strip $(SMOKE_STATUS)),)
SMOKE_FLAGS += --smoke-status $(SMOKE_STATUS)
endif
ifneq ($(strip $(SMOKE_ATTEMPTS)),)
SMOKE_FLAGS += --smoke-attempts $(SMOKE_ATTEMPTS)
endif
ifneq ($(strip $(SMOKE_TIMEOUT)),)
SMOKE_FLAGS += --smoke-timeout $(SMOKE_TIMEOUT)
endif
ifneq ($(strip $(SMOKE_DELAY)),)
SMOKE_FLAGS += --smoke-delay $(SMOKE_DELAY)
endif

.PHONY: help init build up down deploy status logs test clean

help:  ## Print available targets
	@awk 'BEGIN{FS=":.*##"; printf "Usage: make <target> [VAR=value ...]\n\nTargets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init:  ## Seed state dir + initial upstream.conf so the first 'up' has a target
	@mkdir -p $(STATE_DIR) $(dir $(UPSTREAM_CONF))
	@if [ ! -s $(UPSTREAM_CONF) ]; then printf 'server app:8000;\n' > $(UPSTREAM_CONF); fi
	@if [ ! -s $(STATE_FILE) ]; then printf 'blue\n' > $(STATE_FILE); fi

build:  ## Build the application image (CI calls this after pulling)
	$(COMPOSE) build

up: init  ## Bring up the initial blue + proxy stack
	$(COMPOSE) up -d --wait app proxy

down:  ## Stop everything (defeats zero-downtime; teardown only)
	$(COMPOSE) down

deploy: init  ## Run a zero-downtime blue/green rollout end-to-end
	cd $(ROOT) && \
		ROLLOUT_PROJECT_NAME=$(PROJECT_NAME) \
		ROLLOUT_COMPOSE_FILE=$(COMPOSE_FILE) \
		ROLLOUT_STATE_PATH=$(STATE_FILE) \
		ROLLOUT_UPSTREAM_CONF=$(UPSTREAM_CONF) \
		PYTHONPATH=$(ROOT) \
		$(PYTHON) -m $(ROLLOUT_MODULE) $(SMOKE_FLAGS)

status:  ## Print which color is live + the running containers
	@printf 'active color: '
	@cat $(STATE_FILE) 2>/dev/null || printf 'blue (no state file yet)\n'
	@$(COMPOSE) ps

logs:  ## Tail logs for app, app_green, and the proxy
	$(COMPOSE) logs -f --tail=100

test:  ## Run the rollout unit-test suite (no docker required)
	cd $(ROOT) && PYTHONPATH=$(ROOT) $(PYTHON) -m pytest -q

clean:  ## Remove on-host rollout state (does NOT touch containers)
	rm -rf $(STATE_DIR)
	rm -f $(UPSTREAM_CONF)
