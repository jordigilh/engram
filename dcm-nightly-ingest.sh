#!/usr/bin/env bash
# Legacy nightly DCM ingestion wrapper. Deployment-local repository mappings,
# checkout paths, and service settings are read by engram.flows.dcm from
# ~/.engram/projects.toml; this script intentionally contains no DCM inventory.
#
# Scheduled via launchd at 1:30am, before nightly-learn.py --project dcm at 2:30am.
set -euo pipefail

FLOWS_CMD="${HOME}/.engram/venv/bin/engram-flows-dcm"
LOG_PREFIX="[dcm-nightly-ingest]"

echo "${LOG_PREFIX} Starting DCM nightly ingestion at $(date)"
echo "${LOG_PREFIX} Running backfill: docs, issues, code"
"${FLOWS_CMD}" --mode backfill --apps docs issues code "$@"

echo "${LOG_PREFIX} Backfill complete at $(date)"
