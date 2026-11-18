FROM ghcr.io/vectorize-io/hindsight:1.2.1
RUN uv pip install --python /app/api/.venv/bin/python "google-cloud-aiplatform>=1.38"
