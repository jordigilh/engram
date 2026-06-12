#!/usr/bin/env bash
# One-shot migration: export memories from container → import into native instance.
#
# Prerequisites:
#   - Container running on port 8888 (source)
#   - Native instance running on port 8889 (target)
#
# Strategy:
#   - cursor-memory: export all facts via API, re-ingest as verbatim chunks (zero LLM cost)
#   - kubernaut-docs: re-run ingest-docs.py against native (zero LLM cost)
#   - kubernaut-issues: re-run ingest-issues.py against native (zero LLM cost)

set -euo pipefail

SOURCE_URL="${SOURCE_URL:-http://localhost:8888}"
TARGET_URL="${TARGET_URL:-http://localhost:8889}"
EXPORT_DIR="$HOME/.hindsight/migration-export"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$EXPORT_DIR"

echo "=== Hindsight Migration: Container → Native ==="
echo "  Source: $SOURCE_URL (container)"
echo "  Target: $TARGET_URL (native)"
echo ""

# --- Phase 1: Export cursor-memory facts ---
echo "[1/5] Exporting cursor-memory facts from container..."

python3 -u - "$SOURCE_URL" "$EXPORT_DIR" <<'PYEXPORT'
import json, sys, urllib.request

source_url = sys.argv[1]
export_dir = sys.argv[2]
bank = "cursor-memory"

all_items = []
offset = 0
limit = 100

while True:
    url = f"{source_url}/v1/default/banks/{bank}/memories/list?limit={limit}&offset={offset}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    items = data.get("items", [])
    if not items:
        break
    all_items.extend(items)
    offset += limit
    if len(items) < limit:
        break

output_file = f"{export_dir}/{bank}.json"
with open(output_file, "w") as f:
    json.dump(all_items, f)

print(f"  Exported {len(all_items)} facts to {output_file}")
PYEXPORT

# --- Phase 2: Create banks on native instance ---
echo ""
echo "[2/5] Creating banks on native instance..."

for bank in cursor-memory kubernaut-docs kubernaut-issues; do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
        "$TARGET_URL/v1/default/banks/$bank" \
        -H "Content-Type: application/json" \
        -d "{\"description\": \"Migrated from container\"}")
    if [ "$http_code" = "200" ] || [ "$http_code" = "201" ] || [ "$http_code" = "409" ]; then
        echo "  Bank '$bank': OK (HTTP $http_code)"
    else
        echo "  Bank '$bank': FAILED (HTTP $http_code)"
        exit 1
    fi
done

# Configure extraction modes
curl -s -X PATCH "$TARGET_URL/v1/default/banks/cursor-memory/config" \
    -H "Content-Type: application/json" \
    -d '{"updates": {"retain_extraction_mode": "verbatim"}}' > /dev/null

curl -s -X PATCH "$TARGET_URL/v1/default/banks/kubernaut-docs/config" \
    -H "Content-Type: application/json" \
    -d '{"updates": {"retain_extraction_mode": "chunks", "retain_chunk_size": 1200}}' > /dev/null

curl -s -X PATCH "$TARGET_URL/v1/default/banks/kubernaut-issues/config" \
    -H "Content-Type: application/json" \
    -d '{"updates": {"retain_extraction_mode": "chunks", "retain_chunk_size": 1200}}' > /dev/null

echo "  Extraction modes configured"

# --- Phase 3: Import cursor-memory facts ---
echo ""
echo "[3/5] Importing cursor-memory facts into native..."

python3 -u - "$TARGET_URL" "$EXPORT_DIR" <<'PYIMPORT'
import json, sys, urllib.request, time

target_url = sys.argv[1]
export_dir = sys.argv[2]
bank = "cursor-memory"

input_file = f"{export_dir}/{bank}.json"
with open(input_file) as f:
    all_items = json.load(f)

batch_size = 10
imported = 0
errors = 0

for i in range(0, len(all_items), batch_size):
    batch = all_items[i:i+batch_size]
    payload = {
        "items": [{"content": item["text"]} for item in batch]
    }

    url = f"{target_url}/v1/default/banks/{bank}/memories"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        imported += len(batch)
    except Exception as e:
        errors += len(batch)
        print(f"  ERROR at batch {i//batch_size}: {e}", file=sys.stderr)

    if (imported + errors) % 100 == 0 or (i + batch_size) >= len(all_items):
        print(f"  [{imported + errors}/{len(all_items)}] imported={imported} errors={errors}")

print(f"  Done: {imported} imported, {errors} errors")
PYIMPORT

# --- Phase 4: Re-ingest docs and issues ---
echo ""
echo "[4/5] Re-ingesting kubernaut-docs..."
python3 -u "$SCRIPT_DIR/ingest-docs.py" \
    --docs-dir ~/go/src/github.com/jordigilh/kubernaut-docs/docs \
    --hindsight-url "$TARGET_URL" 2>&1 | tail -3

echo ""
echo "[5/5] Re-ingesting kubernaut-issues..."
python3 -u "$SCRIPT_DIR/ingest-issues.py" \
    --hindsight-url "$TARGET_URL" 2>&1 | tail -3

# --- Summary ---
echo ""
echo "=== Migration Complete ==="
echo ""
echo "Verify with:"
echo "  curl -s $TARGET_URL/v1/default/banks | python3 -c \"import json,sys; d=json.load(sys.stdin); [print(f\\\"  {b['bank_id']}: {b['fact_count']} facts\\\") for b in d['banks']]\""
echo ""
echo "Next steps:"
echo "  1. Stop the container: /opt/podman/bin/podman stop hindsight"
echo "  2. Update native to port 8888 and install launchd service"
echo "  3. Verify Cursor MCP still works"
