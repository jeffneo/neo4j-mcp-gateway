# IAM bundle (skeleton)

Identity & Access Management use case for the gateway. Ships with a small demo
dataset and three **stub** tools to fill in (`cypher:` is a TODO).

```bash
# 1. load the demo data (uses your configured Neo4j)
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
  -f bundles/iam/data/iam_demo.cypher

# 2. implement each tool, testing as you go (no gateway restart)
ACTIVE_BUNDLE=iam uv run python scripts/try_tool.py effective_access user_id=U-1001
ACTIVE_BUNDLE=iam uv run python scripts/try_tool.py sod_violations
ACTIVE_BUNDLE=iam uv run python scripts/try_tool.py privilege_paths user_id=U-1002

# 3. check every tool runs
ACTIVE_BUNDLE=iam uv run python scripts/validate_bundle.py

# 4. serve it
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway     # or: uv run neo4j-mcp-gateway --bundle iam
```

Tools to build: `effective_access`, `sod_violations`, `privilege_paths` (hints
are in each file). Planted in the data: an SoD violation (U-1001), a privileged
user (U-1002), and a disabled-but-entitled account (U-1003). Swap in your own
IAM data/tools by editing `data/` and `tools/`.
