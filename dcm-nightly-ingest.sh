#!/usr/bin/env bash
# Nightly DCM ingestion: shallow-clone upstream repos to /tmp, run CocoIndex
# backfill against the fresh clones, then clean up.
#
# Scheduled via launchd at 1:30am, before nightly-learn.py --project dcm at 2:30am.
set -euo pipefail

CLONE_ROOT="/tmp/dcm-repos"
ORG="dcm-project"
VENV_PYTHON="${HOME}/.hindsight/venv/bin/python3"
FLOWS_SCRIPT="${HOME}/.hindsight/dcm-cocoindex-flows.py"
LOG_PREFIX="[dcm-nightly-ingest]"

# Every active DCM repo and its corresponding env var for dcm-cocoindex-flows.py.
# Format: ENV_VAR_NAME=github-repo-name
declare -a REPO_MAP=(
  "DCM_ARCHITECTURE_DIR=dcm"
  "DCM_DOCS_DIR=dcm-project.github.io"
  "DCM_ENHANCEMENTS_DIR=enhancements"
  "DCM_CONTROL_PLANE_DIR=control-plane"
  "DCM_CLI_DIR=cli"
  "DCM_KUBEVIRT_SP_DIR=kubevirt-service-provider"
  "DCM_K8S_CONTAINER_SP_DIR=k8s-container-service-provider"
  "DCM_ACM_CLUSTER_SP_DIR=acm-cluster-service-provider"
  "DCM_THREE_TIER_SP_DIR=three-tier-app-demo-service-provider"
  "DCM_UTILITIES_DIR=utilities"
  "DCM_SHARED_WORKFLOWS_DIR=shared-workflows"
)

cleanup() {
  echo "${LOG_PREFIX} Cleaning up ${CLONE_ROOT}"
  rm -rf "${CLONE_ROOT}"
}
trap cleanup EXIT

echo "${LOG_PREFIX} Starting DCM nightly ingestion at $(date)"

rm -rf "${CLONE_ROOT}"
mkdir -p "${CLONE_ROOT}"

failed=0
for entry in "${REPO_MAP[@]}"; do
  env_var="${entry%%=*}"
  repo="${entry#*=}"
  dest="${CLONE_ROOT}/${repo}"

  echo "${LOG_PREFIX} Cloning ${ORG}/${repo} → ${dest}"
  if ! git clone --depth 1 --quiet "https://github.com/${ORG}/${repo}.git" "${dest}" 2>&1; then
    echo "${LOG_PREFIX} WARNING: failed to clone ${ORG}/${repo}, skipping"
    failed=$((failed + 1))
    continue
  fi

  export "${env_var}=${dest}"
done

if [ "${failed}" -gt 0 ]; then
  echo "${LOG_PREFIX} ${failed} repo(s) failed to clone"
fi

export HINDSIGHT_URL="${HINDSIGHT_URL:-http://localhost:8888}"
export COCOINDEX_PG_URL="${COCOINDEX_PG_URL:-postgresql://hindsight:hindsight@localhost:5432/hindsight}"
export COCOINDEX_DB="${COCOINDEX_DB:-${HOME}/.hindsight/dcm-cocoindex.db}"

echo "${LOG_PREFIX} Running backfill: docs + code + issues"
"${VENV_PYTHON}" "${FLOWS_SCRIPT}" --mode backfill --apps docs code issues

echo "${LOG_PREFIX} Backfill complete at $(date)"
