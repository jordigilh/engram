FROM ghcr.io/vectorize-io/hindsight:1.2.0
RUN uv pip install --python /app/api/.venv/bin/python "google-cloud-aiplatform>=1.38"
