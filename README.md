# Archaeo-Code

Code archeology for legacy systems. Archaeo-Code parses a repository into a knowledge graph of
functions, calls, SQL statements, tables, database triggers and git history. You can then trace a
request down to the rows it changes, see who wrote a rule and in which commit, overlay what a
running system actually did, and ask questions whose answers cite the exact lines they rest on.

**Live demo:** <https://ai-legacy-one.vercel.app>

The demo is read-only and shows one repository, `core-billing-monolith`. That repository is
synthetic: its code, authors, tickets and twelve years of history were written for this project
(see `examples/demo-monolith/`). To analyse your own code, [run it yourself](#run-it-yourself).

## What you get

| View | What it answers |
| --- | --- |
| Overview | Where execution starts, which tables carry the most code, the oldest surviving code, the most complex and most called functions, functions with no caller, code only one person has written |
| Explore | Search by name, by `File.java:123`, or by vocabulary; source, callers, callees, data access, history and line ownership of any node |
| Sequence | The calls an entry point can make, in source order, down to the SQL it runs and the triggers the database fires |
| Data lineage | From entry points through code and SQL to a table, and on to the side effects triggers cause |
| Topology | Dependencies between directories and tables |
| Runtime | Calls and data access reported by a running system, laid over the static graph: what was seen, what was never seen, what only exists at runtime |
| Ask | Questions in plain language, answered from the graph with a citation on every statement |

## How it works

1. **Parse.** Tree-sitter grammars read Java, Python, JavaScript / TypeScript (including JSX and
   TSX) and C / C++. SQL files are split per statement for PostgreSQL, MySQL, SQL Server and
   Oracle syntax. SQL written inside application code is extracted and analysed as well.
2. **Resolve.** Calls are linked to their targets with a confidence, classes to their supertypes,
   functions and statements to the tables they read or write, tables to the triggers they fire.
   Entry points (HTTP routes, scheduled jobs, `main` functions, message handlers, command-line
   commands) are recognised from common annotations, decorators and registrations.
3. **Read the history.** `git log` and `git blame` attach every function, class and file to the
   commits that still own lines in it, and commits to their authors. For repositories on GitHub
   the pull requests named in commit messages are fetched and attached to their commits.
4. **Store.** Everything goes into one graph:

   ```
   (Repository)-[:CONTAINS]->(File)-[:CONTAINS]->(Class)-[:CONTAINS]->(Function)
   (Function)-[:CONTAINS]->(DatabaseQuery)
   (Function)-[:CALLS]->(Function)
   (Class)-[:INHERITS_FROM]->(Class)
   (Function|DatabaseQuery)-[:READS_FROM_DB|MUTATES_TABLE]->(DatabaseTable)
   (DatabaseTable)-[:FIRES_TRIGGER]->(Function)
   (File|Class|Function)-[:TOUCHED_BY]->(Commit)-[:AUTHORED_BY]->(Developer)
   (Function)-[:OBSERVED_CALL]->(Function)                          runtime telemetry
   (Function)-[:OBSERVED_READ|OBSERVED_WRITE]->(DatabaseTable)      runtime telemetry
   ```

5. **Index.** Each function, class, statement, table and commit is embedded for search by
   vocabulary.
6. **Answer.** A question selects starting nodes (names and locations it mentions, plus search
   hits), the graph around them is collected as numbered evidence, and the answer is written from
   that evidence only. Every citation in the answer is then checked against the evidence and the
   source files; the result ("34 of 34 citations verified") is shown with the answer.

| Directory | Contents |
| --- | --- |
| `parsers/` | Language parsers and SQL analysis |
| `ingestion/` | Pipeline: checkout, discovery, parsing, resolution, git history, insights |
| `graph_engine/` | Graph schema, SQLite and Neo4j stores, traversal algorithms, snapshots |
| `rag_engine/` | Embeddings, vector index, retrieval, answer generation, citation checks |
| `backend/` | FastAPI application, command line, telemetry intake, jobs |
| `frontend/` | Next.js web interface |
| `examples/demo-monolith/` | The synthetic demo repository and its pre-computed graph |
| `tests/`, `tools/cypher-lint/` | Test suite; static check of the Cypher sent to Neo4j |

## Run it yourself

You need Python 3.11 or newer, git, and Node.js 22.

```bash
git clone https://github.com/kennedy-blip/ai-legacy.git
cd ai-legacy

python3 -m venv .venv
. .venv/bin/activate
pip install -e .
archaeo serve
```

The API now listens on <http://127.0.0.1:8000>; its interactive documentation is at
<http://127.0.0.1:8000/api/v1/docs>. The install has to be editable (`-e`): the demo repository is
read from `examples/` in the checkout.

In a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://localhost:3000>. On first start the server builds the demo repository with its git
history under `data/` and analyses it, so there is something to look at straight away. Set
`ARCHAEO_SEED_DEMO=false` to start empty.

**Add a repository** with the form on the home page, or from the command line:

```bash
archaeo ingest https://github.com/spring-projects/spring-petclinic
archaeo repos
archaeo ask spring-petclinic-c57d758f "What does OwnerController.processCreationForm do?"
```

`archaeo repos` prints the identifier to use with `ask`. That repository (57 source files, about
1,000 commits) takes a few seconds. A private repository needs an access token: use "Branch or private repository" in the form. The token is used for the
clone and is not stored. A directory on the server can be analysed when it lies under
`ARCHAEO_ALLOWED_LOCAL_ROOTS`, or with `archaeo ingest --allow-local /path/to/repo`.

### Larger installations

`pip install -e .` gives a single process with the graph in SQLite (`data/graph.sqlite3`) and the
search index in memory, rebuilt from the graph at each start. The extras add other backends:

```bash
pip install -e ".[server]"     # Neo4j driver, Qdrant client, Celery, Redis client
```

| Setting | Effect |
| --- | --- |
| nothing more | The search index is kept on disk by embedded Qdrant under `data/qdrant` |
| `ARCHAEO_QDRANT_URL=http://…` | Search index in a Qdrant server |
| `ARCHAEO_GRAPH_BACKEND=neo4j` with `ARCHAEO_NEO4J_URI`, `_USER`, `_PASSWORD` | Graph in Neo4j |
| `ARCHAEO_JOB_BACKEND=celery` with `ARCHAEO_REDIS_URL` | Ingestion in worker processes started with `archaeo worker`; this needs a Qdrant server |

Read [what has been run](#what-has-and-has-not-been-run) before relying on the last three.

To serve the web interface without the development server, the address of the API must be known
when it is built:

```bash
cd frontend
ARCHAEO_BACKEND_URL=http://127.0.0.1:8000 npm run build
npm start
```

## Configuration

Settings are environment variables with the prefix `ARCHAEO_`, or lines in a `.env` file in the
directory the server is started from. [`.env.example`](.env.example) lists all of them with their
defaults; `backend/settings.py` is the definition. The ones most often needed:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARCHAEO_DATA_DIR` | `data` | Graph database, search index, cloned repositories |
| `ARCHAEO_API_TOKEN` | not set | When set, every request must send `Authorization: Bearer <token>`, except the health check `/api/v1/health` and the API reference (`/api/v1/docs`, `/api/v1/openapi.json`). Required when `ARCHAEO_ENVIRONMENT=production` |
| `ARCHAEO_ALLOWED_GIT_HOSTS` | any | Hosts repositories may be cloned from, e.g. `github.com,git.example.com` |
| `ARCHAEO_ALLOWED_LOCAL_ROOTS` | none | Server directories from which local repositories may be analysed |
| `ARCHAEO_READ_ONLY` | `false` | Refuse ingestion, deletion and telemetry intake |
| `ARCHAEO_LLM_PROVIDER` | `none` | `openai`, `anthropic` or `ollama`; with `ARCHAEO_LLM_MODEL` and `ARCHAEO_LLM_API_KEY` |
| `ARCHAEO_EMBEDDING_PROVIDER` | `hashing` | `openai` uses an OpenAI-compatible embedding endpoint instead of the built-in lexical embedding |
| `ARCHAEO_GITHUB_TOKEN` | not set | Raises the GitHub rate limit for fetching pull requests |

The web interface reads one variable, `ARCHAEO_BACKEND_URL` (default `http://127.0.0.1:8000`):
where it forwards `/api/*`. When the API is protected by `ARCHAEO_API_TOKEN`, the interface asks
for the token once and keeps it in the local storage of the browser.

## Command line

```
archaeo serve [--host HOST] [--port PORT] [--reload]     run the HTTP API (default 127.0.0.1:8000)
archaeo ingest SOURCE [--name NAME] [--branch BRANCH] [--include PREFIX] [--allow-local]
archaeo repos                                            list analysed repositories
archaeo ask REPOSITORY "QUESTION" [--json]               answer in the terminal
archaeo export REPOSITORY FILE [--no-runtime]            write a graph snapshot (.gz compresses)
archaeo import FILE [--sources DIR]                      load a snapshot into this instance
archaeo demo [--force] [--write-snapshot]                rebuild the demo repository
archaeo worker [--concurrency N]                         Celery ingestion worker
```

A snapshot is a text file with one JSON record per line that holds the whole graph of one
repository. It lets you analyse code on one machine and browse the result on another that has no
access to the code. Without `--sources` the importing instance shows the graph but no source text.

## HTTP API

Everything the interface shows comes from the API under `/api/v1`; `/api/v1/docs` lists every
endpoint with its parameters.

```bash
API=http://127.0.0.1:8000/api/v1
REPO=core-billing-monolith-56caf2b5

curl "$API/repositories"
curl "$API/repositories/$REPO/search?q=lateFee"
curl "$API/repositories/$REPO/entry-points"

# Analyse a repository; poll $API/jobs for progress
curl -X POST "$API/repositories" -H 'Content-Type: application/json' \
  -d '{"source": "https://github.com/spring-projects/spring-petclinic"}'

# Ask. Without "stream": false the response is newline-delimited JSON events.
curl -X POST "$API/repositories/$REPO/explain" -H 'Content-Type: application/json' \
  -d '{"question": "Why does FeeCalculator.lateFee cap the fee?", "stream": false}'
```

A link can carry a question to the Ask view: `/r/<repository>?tab=ask&q=<question>`, optionally
with `&node=<node id>` to ask about one node.

## Runtime telemetry

Static analysis cannot see reflection, dependency injection or dynamic dispatch. A running system
can report what it did, and Archaeo-Code lays those reports over the graph. There is no agent in
this repository: you send events from your own instrumentation.

```bash
curl -X POST "$API/repositories/$REPO/telemetry/events" -H 'Content-Type: application/json' -d '{
  "source": "my-agent",
  "events": [
    {"type": "call",  "caller": "PaymentService.applyPayment", "callee": "LedgerDao.insertEntry",
     "count": 3, "duration_ms": 3.2, "error": false},
    {"type": "query", "function": "LedgerDao.insertEntry", "table": "tbl_ledger",
     "operation": "INSERT", "duration_ms": 1.1}
  ]
}'
```

Functions are matched by name: `Class.method`, a fully qualified `com.acme.Class.method`,
`ns::Class::method`, or a `File.java:123` location. Names that match nothing are counted and
returned as `unmatched_symbols`.

| Endpoint | Purpose |
| --- | --- |
| `POST …/telemetry/events` | The JSON events above, up to 5,000 per request |
| `POST …/telemetry/otlp` | The JSON body an OpenTelemetry exporter posts to `/v1/traces`. Spans are linked to their parents within one request only |
| `GET …/telemetry/summary` | What the Runtime view shows |
| `GET …/telemetry/live` | Server-sent events with newly observed relationships |
| `POST …/telemetry/simulate` | Generates traffic by walking the static graph, to show what the overlay looks like. It is labelled as simulated everywhere and proves nothing about a real system |
| `DELETE …/telemetry` | Removes all observations of the repository |

## Answers and citations

Without a language model (the default), an answer is a report assembled from graph facts: callers,
callees, tables read and written, triggers, the commits that last changed the code and their
messages. Each line carries a citation such as `[FeeCalculator.java:25]` or `[commit 64c96ce8]`.

With `ARCHAEO_LLM_PROVIDER` set, a model writes the answer from the same numbered evidence. In
both cases every citation is checked afterwards: does it point into the evidence that was
retrieved, and does the file have those lines? Citations that fail are marked in the interface
and counted in `citation_summary`.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests -q
ruff check backend parsers graph_engine ingestion rag_engine tests examples
mypy backend parsers graph_engine ingestion rag_engine

(cd tools/cypher-lint && npm ci)
python -m graph_engine.cypher_queries | node tools/cypher-lint/lint.mjs graph_engine/cypher/schema.cypher

cd frontend && npm run lint && npm run typecheck && npm run build
```

The suite needs git. 23 tests run the graph-store contract against Neo4j and are skipped unless
`ARCHAEO_TEST_NEO4J_URI` (with `ARCHAEO_TEST_NEO4J_USER` and `ARCHAEO_TEST_NEO4J_PASSWORD`) points
at a server.

One test rebuilds the demo graph and compares it with the shipped file
`examples/demo-monolith/graph-snapshot.jsonl`. After a change to a parser that alters the graph,
regenerate the file with `archaeo demo --write-snapshot`.

## The hosted demo on Vercel

`vercel.json` declares two services: the Next.js application in `frontend/`, and the FastAPI
application `backend.vercel_app:app`, which receives `/api/*`. A serverless function has no git
and no durable disk, so that entry point starts in read-only mode: it loads the demo graph from
the snapshot file, builds the search index in memory, and answers every write with `403`.

Nothing has to be configured on Vercel. `ARCHAEO_*` variables set in the project are honoured, so
a language model can be enabled there. The project's production branch is `main`.

## What has and has not been run

Run and checked while building this:

- The test suite, the linters and type checks listed above.
- The whole path on the demo repository and on `spring-projects/spring-petclinic`, cloned from
  GitHub: ingestion through the form and the command line, every view, simulated and posted
  telemetry, answers with verified citations, snapshot export and import between two instances.
- SQLite as the graph store; the in-memory index and embedded Qdrant as the search index.
- The read-only deployment on Vercel.
- The parsers for JavaScript, TypeScript and C / C++ in unit tests only. No complete repository in
  those languages was analysed.

Written but never run, because the services were not available where this was built:

- **Neo4j.** The store is implemented and every Cypher statement it can send passes a static
  syntax check, but it has not talked to a Neo4j server.
- **A Qdrant server.** Only the embedded mode ran.
- **Celery workers and Redis.**
- **Answers written by a language model.** No API key was available, so only graph reports were
  produced. The prompt, the streaming and the citation check after generation are untested with a
  real model.

Treat those four as unverified until you have tried them.

## Limits

- Tables are identified by their unqualified, lower-cased name. Two tables with the same name in
  different schemas or databases become one node.
- Calls through reflection, dependency injection containers, function pointers and most dynamic
  dispatch are not resolved. Calls resolved by name alone are marked as possible, not certain.
- A sequence diagram lists every call the code can make in the order it is written. It ignores
  conditions and loops, so it is not a trace of one execution.
- "No caller found" and "written by one person" are lists of candidates. Code reached only
  through a framework or from outside the repository looks unused to static analysis.
- The built-in embedding is lexical: it matches shared vocabulary, including parts of
  identifiers, and does not know that two different words mean the same thing.
- SQL assembled at run time is read as far as its literal parts go. A table whose name comes
  from a variable is not known.
- The hosted demo cannot analyse repositories, accept telemetry or use a language model.

## Licence

No licence has been chosen yet. Until the owner adds one, the code is published without
permission to reuse it.
