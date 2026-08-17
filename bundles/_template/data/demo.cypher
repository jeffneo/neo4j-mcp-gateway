// TODO: Cypher that generates this bundle's demo dataset.
//
// Conventions that pay off (see bundles/ato/data/ato_demo.cypher for a full example):
//   * tag every node with  source:'{{BUNDLE_NAME}}-demo'  and start the script with
//       MATCH (n {source:'{{BUNDLE_NAME}}-demo'}) DETACH DELETE n;
//     so re-running is idempotent and cleanup is one line.
//   * keep IDs neutral (no 'FRAUD'/'BAD' tells); put any ground truth in a flag
//     your detection queries deliberately ignore.
//   * add uniqueness constraints for your key business identifiers.
//
// Load with:
//   cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                -d "$NEO4J_DATABASE" -f bundles/{{BUNDLE_NAME}}/data/demo.cypher

RETURN 'Replace bundles/{{BUNDLE_NAME}}/data/demo.cypher with your dataset.' AS todo;
