#!/usr/bin/env bash
# Start Hindsight as a native macOS process.
# LLM configuration lives in ~/.engram/config.env (outside this repo).

set -euo pipefail

CONFIG="${HOME}/.engram/config.env"
ADC_PATH="${HOME}/.config/gcloud/application_default_credentials.json"
VENV="${HOME}/.engram/hindsight-venv"

if [ ! -f "$CONFIG" ]; then
    echo "Error: ${CONFIG} not found."
    echo "Copy config.env.example to ~/.engram/config.env and fill in your values."
    exit 1
fi

if [ ! -f "$ADC_PATH" ]; then
    echo "Error: GCP credentials not found at ${ADC_PATH}"
    echo "Run: gcloud auth application-default login"
    exit 1
fi

if [ ! -f "$VENV/bin/hindsight-api" ]; then
    echo "Error: Hindsight not installed in $VENV"
    echo "Run: uv venv $VENV --python 3.13"
    echo "     uv pip install --python $VENV/bin/python 'hindsight-api==0.10.0'"
    exit 1
fi

mkdir -p "$HOME/.engram/logs"

set -a
source "$CONFIG"
export GOOGLE_APPLICATION_CREDENTIALS="$ADC_PATH"
export HINDSIGHT_API_PORT="${HINDSIGHT_API_PORT:-8888}"
set +a

exec "$VENV/bin/hindsight-api"
