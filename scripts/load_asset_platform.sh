#!/usr/bin/env bash
# Load the asset_platform bundle in dependency order.
#
#   ./scripts/load_asset_platform.sh
#
# FIVE STEPS, THREE OWNERS. The order is not cosmetic — each step joins to nodes
# the previous one created, and getting it wrong does not fail loudly. It produces
# a graph with missing entitlement edges, which UNDER-grants: callers silently see
# less than they should, and only scripts/check_entitlements.py notices.
#
#   1. platform.cypher                  business graph      (no dependencies)
#   2. ingest_business_hierarchy.py     the HR view         (needs nothing)
#   3. ingest_coverage_teams.py         the coverage view   (needs 1 and 2)
#   4. identity.cypher                  roles and barriers  (needs 1 and 2)
#   5. trading.cypher                   trades and comp     (needs 1, 2 and 4)
#
# Steps 2 and 3 are PROJECTIONS of relational views with authoritative upstream
# providers. Step 4 is the part no view provides: role assignment, research scope
# and information barriers — the edges that exist only to express entitlement.
set -euo pipefail

cd "$(dirname "$0")/.."
# Read only the connection keys, and read them as data: .env also holds command
# lines with spaces, which `source` would try to execute.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      NEO4J_URI|NEO4J_USERNAME|NEO4J_PASSWORD|NEO4J_DATABASE)
        [ -z "${!key:-}" ] && export "$key=$value" ;;
    esac
  done < .env
fi
: "${NEO4J_URI:?set NEO4J_URI in .env}"

DATA=bundles/asset_platform/data
VIEWS=$DATA/views

# Override when cypher-shell is not on the host, e.g. against a container:
#   CYPHER_SHELL="docker exec -i neo4j-ap cypher-shell" ./scripts/load_asset_platform.sh
CYPHER_SHELL=${CYPHER_SHELL:-cypher-shell}
shell() {
  $CYPHER_SHELL -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
                -d "${NEO4J_DATABASE:-neo4j}" < "$1"
}

echo "== 1/5  business graph"
shell $DATA/platform.cypher

echo "== 2/5  business_hierarchy view -> people, units, reporting"
uv run python scripts/ingest_business_hierarchy.py "$VIEWS/business_hierarchy.csv" \
    --bundle asset_platform

echo "== 3/5  coverage_teams view -> teams, membership, covered accounts"
uv run python scripts/ingest_coverage_teams.py "$VIEWS/coverage_teams.csv" \
    --bundle asset_platform

echo "== 4/5  roles, research scope, barriers"
shell $DATA/identity.cypher

echo "== 5/5  trades and compensation"
shell $DATA/trading.cypher

echo
echo "== conformance"
ACTIVE_BUNDLE=asset_platform uv run python scripts/check_entitlements.py
