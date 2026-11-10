// Archaeo-Code knowledge-graph schema for Neo4j 5.x and later.
//
// Every node carries two labels: the shared :Entity label plus one domain label
// (:Repository, :File, :Class, :Function, :DatabaseQuery, :DatabaseTable, :Commit,
// :Developer, :IngestionJob). The shared label lets a single uniqueness constraint
// and a small set of composite indexes serve id, name and path lookups for all of
// them, while analysts can still write natural patterns such as
//   MATCH (f:Function)-[:CALLS]->(g:Function)
//
// Statements are idempotent and are applied by Neo4jGraphStore.ensure_schema().

// Identity: ids are deterministic hashes of (repository, natural key).
CREATE CONSTRAINT entity_id IF NOT EXISTS
FOR (n:Entity) REQUIRE n.id IS UNIQUE;

// "All nodes of label X in repository R" - listings, counts, repository deletion.
CREATE INDEX entity_repo_label IF NOT EXISTS
FOR (n:Entity) ON (n.repository_id, n.label);

// Case-insensitive exact lookup by simple name ("processNightlyReconciliation").
CREATE INDEX entity_repo_name IF NOT EXISTS
FOR (n:Entity) ON (n.repository_id, n.name_lower);

// Case-insensitive exact lookup by qualified name ("LegacyAccountDAO.findFlagged").
CREATE INDEX entity_repo_qualified_name IF NOT EXISTS
FOR (n:Entity) ON (n.repository_id, n.qualified_name_lower);

// "Which scopes of file F contain line L" - resolves citations like Foo.java:304.
CREATE INDEX entity_repo_path IF NOT EXISTS
FOR (n:Entity) ON (n.repository_id, n.path);

// Substring symbol search (CONTAINS / STARTS WITH) from the dashboard search box.
CREATE TEXT INDEX entity_name_text IF NOT EXISTS
FOR (n:Entity) ON (n.name_lower);
