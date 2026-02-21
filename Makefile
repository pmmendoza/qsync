# qsync make compatibility targets
#
# Purpose: keep legacy `make fullsync` operator workflows while delegating to
# the canonical `qsync sync` engine.

QSYNC ?= qsync

# Compatibility inputs
SURVEY ?=
SURVEYS ?=
DIMENSIONS ?=
SCOPE ?=
PENDING_ACTION ?=
EXTRA_SYNC_FLAGS ?=

# Boolean toggles (set to 1 to enable)
YES ?=0
LIVE ?=0
PREVIEW_ITEMS ?=0
PER_DIMENSION ?=0
SKIP_PUBLISH ?=0
REFRESH_WORKBOOKS ?=0
REFRESH_INVENTORY ?=0
NO_REFRESH_INVENTORY ?=0

.PHONY: help fullsync fullsync.items fullsync.js

help:
	@echo "qsync Make targets"
	@echo "  make fullsync [SURVEY=SV_xxx] [SURVEYS='SV_a SV_b'] [YES=1] [LIVE=1]"
	@echo "  make fullsync.items  # DIMENSIONS=items"
	@echo "  make fullsync.js     # DIMENSIONS=js"
	@echo ""
	@echo "Supported compatibility flags:"
	@echo "  DIMENSIONS, SCOPE, PENDING_ACTION, PER_DIMENSION=1, SKIP_PUBLISH=1"
	@echo "  PREVIEW_ITEMS=1 (maps to --force-preview), REFRESH_INVENTORY=1, NO_REFRESH_INVENTORY=1"

fullsync:
	@set -eu; \
	args="sync"; \
	if [ -n "$(SURVEY)" ]; then \
		args="$$args --survey-id $(SURVEY)"; \
	fi; \
	for sid in $(SURVEYS); do \
		args="$$args --survey-id $$sid"; \
	done; \
	if [ -z "$(SURVEY)$(SURVEYS)" ]; then \
		args="$$args --all-focal"; \
	fi; \
	if [ -n "$(DIMENSIONS)" ]; then \
		args="$$args --dimensions $(DIMENSIONS)"; \
	fi; \
	if [ -n "$(SCOPE)" ]; then \
		args="$$args --scope $(SCOPE)"; \
	fi; \
	if [ -n "$(PENDING_ACTION)" ]; then \
		args="$$args --pending-action $(PENDING_ACTION)"; \
	fi; \
	if [ "$(YES)" = "1" ]; then \
		args="$$args --yes"; \
	fi; \
	if [ "$(LIVE)" = "1" ]; then \
		args="$$args --force-live"; \
	fi; \
	if [ "$(PREVIEW_ITEMS)" = "1" ]; then \
		args="$$args --force-preview"; \
	fi; \
	if [ "$(PER_DIMENSION)" = "1" ]; then \
		args="$$args --per-dimension"; \
	fi; \
	if [ "$(SKIP_PUBLISH)" = "1" ]; then \
		args="$$args --skip-publish"; \
	fi; \
	if [ "$(REFRESH_WORKBOOKS)" = "1" ]; then \
		args="$$args --refresh-workbooks"; \
	fi; \
	if [ "$(REFRESH_INVENTORY)" = "1" ]; then \
		args="$$args --refresh-inventory"; \
	fi; \
	if [ "$(NO_REFRESH_INVENTORY)" = "1" ]; then \
		args="$$args --no-refresh-inventory"; \
	fi; \
	if [ -n "$(EXTRA_SYNC_FLAGS)" ]; then \
		args="$$args $(EXTRA_SYNC_FLAGS)"; \
	fi; \
	echo "[make:fullsync] $(QSYNC) $$args"; \
	exec $(QSYNC) $$args

fullsync.items:
	@$(MAKE) fullsync DIMENSIONS=items

fullsync.js:
	@$(MAKE) fullsync DIMENSIONS=js
