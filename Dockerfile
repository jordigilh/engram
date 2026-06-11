FROM ghcr.io/vectorize-io/hindsight:latest
RUN uv pip install --python /app/api/.venv/bin/python "google-cloud-aiplatform>=1.38"
