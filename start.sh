#!/usr/bin/env bash
# Start Hindsight as a native macOS process.
# LLM configuration lives in ~/.hindsight/config.env (outside this repo).

set -euo pipefail

CONFIG="${HOME}/.hindsight/config.env"
ADC_PATH="${HOME}/.config/gcloud/application_default_credentials.json"
VENV="${HOME}/.hindsight/venv"

if [ ! -f "$CONFIG" ]; then
    echo "Error: ${CONFIG} not found."
    echo "Copy config.env.example to ~/.hindsight/config.env and fill in your values."
    exit 1
fi

if [ ! -f "$ADC_PATH" ]; then
    echo "Error: GCP credentials not found at ${ADC_PATH}"
    echo "Run: gcloud auth application-default login"
    exit 1
fi

if [ ! -f "$VENV/bin/hindsight-api" ]; then
    echo "Error: Hindsight not installed in $VENV"
    echo "Run: uv pip install --python $VENV/bin/python 'hindsight-api[all]' 'google-cloud-aiplatform>=1.38'"
    exit 1
fi

mkdir -p "$HOME/.hindsight/logs"

set -a
source "$CONFIG"
export GOOGLE_APPLICATION_CREDENTIALS="$ADC_PATH"
export HINDSIGHT_API_PORT="${HINDSIGHT_API_PORT:-8888}"
set +a

exec "$VENV/bin/hindsight-api"
