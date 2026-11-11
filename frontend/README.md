# Archaeo-Code web interface

The Next.js application that shows what the Archaeo-Code API knows about a repository. What the
product does and how to run the whole thing is in the [README of the repository](../README.md).

## Run

The API has to be running first (`archaeo serve`, see the main README). Then:

```bash
npm ci
npm run dev          # http://localhost:3000
```

Node.js 22 is required. The browser only ever talks to the origin that served the page; Next
forwards `/api/*` to the API at `ARCHAEO_BACKEND_URL` (default `http://127.0.0.1:8000`).

```bash
ARCHAEO_BACKEND_URL=http://127.0.0.1:8001 npm run dev      # an API on another port
```

For `npm run build` followed by `npm start`, the variable has to be set for the build: the
forwarding rule is fixed at that point. On Vercel no rule is registered, because the platform
routes `/api/*` to the Python service itself (see `vercel.json` in the repository root).

## Checks

```bash
npm run lint
npm run typecheck    # generates the route types, then runs tsc
npm run build
```

## Layout

| Path | Contents |
| --- | --- |
| `src/app/` | Routes: the home page and `r/[repo]` for one repository |
| `src/lib/` | API client, types of the API responses, data hooks, formatting |
| `src/ui/home/` | Repository list and the form that starts an analysis |
| `src/ui/repository/` | The seven views of a repository and what they share |
| `src/ui/visualizers/` | SVG diagrams: call graph, lineage, sequence, topology |
| `src/ui/common/`, `src/ui/shell/` | Shared building blocks, header, API-token prompt |

The address holds the state of a repository page: `?tab=` selects the view and `&node=` the
selected node, so every view can be linked to. `?tab=ask&q=<question>` opens the Ask view and asks
the question once.

`AGENTS.md` is written by Next.js for coding assistants and is kept as generated.
