# {{BUNDLE_NAME}} bundle

A use-case bundle for the Neo4j MCP gateway. To build it out:

1. **`bundle.yaml`** — set `description` and (importantly) `instructions`.
2. **`data/demo.cypher`** — write your dataset generator; load it with `cypher-shell`.
3. **`tools/*.yaml`** — add curated tools. Iterate fast, no gateway restart:
   ```bash
   ACTIVE_BUNDLE={{BUNDLE_NAME}} uv run python scripts/try_tool.py <tool> key=value
   ```
4. **Validate** every tool runs against a live DB:
   ```bash
   ACTIVE_BUNDLE={{BUNDLE_NAME}} uv run python scripts/validate_bundle.py
   ```
5. **Serve it:** `ACTIVE_BUNDLE={{BUNDLE_NAME}} uv run neo4j-mcp-gateway`
   (or add `--bundle {{BUNDLE_NAME}}`), then connect from your MCP client.

Credentials: inherit the repo-root `.env`, or drop a git-ignored `.env` here to
point this bundle at its own Neo4j instance.
