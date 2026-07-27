#!/usr/bin/env bash
# Shared launchd wrapper: sources ~/.hindsight/config.env at process startup
# and then exec's the real command.
#
# Why this exists: values like VERTEXAI_PROJECT/GOOGLE_CLOUD_PROJECT must be
# injected into the process environment at runtime, not hardcoded into a
# plist file on disk (even a local, non-git-tracked one under
# ~/Library/LaunchAgents/) -- config.env is the single source of truth, the
# same one ./start.sh already sources for the hindsight-api dev flow. Baking
# the value into N separate generated plists means N places to update (and N
# places a secret sits in plaintext) instead of one.
#
# Used as the first argument of ProgramArguments in every launchd plist under
# launchd/ that runs a script needing Vertex AI config (nightly-learn.py,
# cocoindex-flows.py, dcm-nightly-ingest.sh, hindsight-api itself):
#   ProgramArguments = [
#     "__HOME__/.hindsight/with-config-env.sh",
#     "__HOME__/.hindsight/venv/bin/python3",
#     "__HOME__/.hindsight/nightly-learn.py",
#     "--mode", "nightly"
#   ]
set -euo pipefail

CONFIG="${HOME}/.hindsight/config.env"

if [ -f "$CONFIG" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG"
  set +a
fi

# Always the real ADC default path, regardless of what (if anything)
# config.env sets for this -- matches start.sh's override for the same
# reason: config.env's GOOGLE_APPLICATION_CREDENTIALS may point elsewhere
# (e.g. a service-account key path) that doesn't exist on this machine.
export GOOGLE_APPLICATION_CREDENTIALS="${HOME}/.config/gcloud/application_default_credentials.json"

exec "$@"
