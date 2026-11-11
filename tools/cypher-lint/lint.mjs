#!/usr/bin/env node
/**
 * Lints Cypher with Neo4j's official parser and semantic analyser
 * (@neo4j-cypher/language-support - the engine behind the Neo4j VS Code extension).
 *
 * Input:
 *   - stdin: JSON object {name: statement} (from `python -m graph_engine.cypher_queries`)
 *   - argv:  any number of .cypher files; statements are split on ';'
 *
 * This is a *static* check: it proves the statements parse and are semantically
 * well-formed (no undefined variables, valid clause composition). It does not
 * execute them - runtime behaviour is covered by the GraphStore contract tests
 * running against a live Neo4j in CI.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { lintCypherQuery } = require("@neo4j-cypher/language-support");

const SEVERITY_ERROR = 1;

function lint(statement) {
  const result = lintCypherQuery(statement, {});
  const diagnostics = Array.isArray(result) ? result : (result?.diagnostics ?? []);
  return diagnostics.map((d) => ({
    severity: d.severity,
    message: d.message,
    line: (d.range?.start?.line ?? 0) + 1,
    column: (d.range?.start?.character ?? 0) + 1,
  }));
}

function selfTest() {
  // Guard against the linter silently accepting everything (e.g. after an API change).
  const syntax = lint("MATCH (a:Entity {id: $id})-[r:CALLS]->(b WHERE RETURN a");
  const semantic = lint("MATCH (a:Entity) RETURN b");
  const clean = lint("MATCH (a:Entity {id: $id}) RETURN a.id AS id");
  const ok =
    syntax.some((d) => d.severity === SEVERITY_ERROR) &&
    semantic.some((d) => d.severity === SEVERITY_ERROR) &&
    clean.length === 0;
  if (!ok) {
    console.error("cypher-lint self-test failed: the linter did not behave as expected");
    console.error(JSON.stringify({ syntax, semantic, clean }, null, 2));
    process.exit(2);
  }
}

function splitCypherFile(path) {
  const text = readFileSync(path, "utf8");
  const statements = {};
  let index = 0;
  for (const chunk of text.split(";")) {
    const body = chunk
      .split("\n")
      .filter((line) => !line.trimStart().startsWith("//"))
      .join("\n")
      .trim();
    if (!body) continue;
    const title = chunk.split("\n").find((line) => line.trimStart().startsWith("// @name "));
    const name = title ? title.replace(/^\s*\/\/ @name\s+/, "").trim() : `#${index}`;
    statements[`${path}:${name}`] = body;
    index += 1;
  }
  return statements;
}

function readStdin() {
  if (process.stdin.isTTY) return {};
  const raw = readFileSync(0, "utf8").trim();
  return raw ? JSON.parse(raw) : {};
}

selfTest();

const statements = { ...readStdin() };
for (const path of process.argv.slice(2)) Object.assign(statements, splitCypherFile(path));

const names = Object.keys(statements);
if (names.length === 0) {
  console.error("cypher-lint: no statements supplied");
  process.exit(2);
}

let errors = 0;
let warnings = 0;
for (const name of names) {
  for (const d of lint(statements[name])) {
    const level = d.severity === SEVERITY_ERROR ? "error" : "warning";
    if (level === "error") errors += 1;
    else warnings += 1;
    console.error(`${level}: ${name} (${d.line}:${d.column}) ${d.message}`);
    if (level === "error") console.error(statements[name].replace(/^/gm, "    "));
  }
}

console.log(`cypher-lint: ${names.length} statements checked, ${errors} errors, ${warnings} warnings`);
process.exit(errors > 0 ? 1 : 0);
