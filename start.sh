#!/usr/bin/env bash
# Start the Hindsight container.
# LLM configuration lives in ~/.hindsight/config.env (outside this repo).

set -euo pipefail

CONFIG="${HOME}/.hindsight/config.env"
DATA_DIR="${HOME}/.hindsight/data"
ADC_PATH="${HOME}/.config/gcloud/application_default_credentials.json"

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

mkdir -p "$DATA_DIR"

/opt/podman/bin/podman rm -f hindsight 2>/dev/null || true

/opt/podman/bin/podman run -d --name hindsight \
  --restart unless-stopped \
  -p 8888:8888 -p 9999:9999 \
  --env-file "$CONFIG" \
  -v "${ADC_PATH}":/tmp/keys/adc.json:ro \
  -v "${DATA_DIR}":/home/hindsight/.pg0 \
  hindsight-vertexai

echo "Hindsight started. API: http://localhost:8888 | UI: http://localhost:9999"
