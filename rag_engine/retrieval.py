"""Graph-RAG retrieval: find the scopes a question is about, then walk the graph around them.

Vector similarity alone answers "which code *talks about* this?". Understanding
legacy behaviour needs the surroundings too: who calls the code, what it writes,
which trigger fires as a consequence, and which commit explains why it looks the
way it does. The retriever therefore works in two steps:

1. **Seed** - scopes named in the question (``File.java:304``, ``Class.method``,
   table names, commit hashes), the node the user is looking at, and the best
   hybrid vector matches.
2. **Expand** - for every seed, follow the knowledge graph: callers, callees,
   embedded SQL, tables, trigger side effects and the commits that last changed
   its surviving lines.

The result is a :class:`ContextPack` of numbered evidence items, each with an
exact file/line span or commit hash that an answer can cite and that
:mod:`rag_engine.citations` can verify afterwards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from graph_engine import locator
from graph_engine.algorithms import DEFAULT_MIN_CONFIDENCE, Neighbour, NodeContext, node_context
from graph_engine.models import GraphNode
from graph_engine.schema import Direction, NodeLabel, RelType
from graph_engine.store import GraphStore
from rag_engine.source import SourceReader, numbered
from rag_engine.vector_index import VectorIndex, VectorIndexError

logger = logging.getLogger(__name__)

_BACKTICK_RE: Final = re.compile(r"`([^`\n]{2,120})`")
_IDENTIFIER_RE: Final = re.compile(
    r"\b(?:[A-Za-z_]\w*(?:\.|::|#))+[A-Za-z_]\w*\b"  # Class.method, ns::fn, Class#method
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b"  # CamelCase
    r"|\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"  # camelCase
    r"|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"  # snake_case
    r"|\b[A-Za-z_]\w{2,}(?=\()"  # name(
)
_HASH_RE: Final = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_SCOPES: Final = frozenset({NodeLabel.FUNCTION, NodeLabel.CLASS, NodeLabel.DATABASE_TABLE})
_ROLE_ORDER: Final = {
    "focus": 0,
    "sql": 1,
    "side-effect": 2,
    "history": 3,
    "data": 4,
    "caller": 5,
    "callee": 6,
}

#: Score given to scopes the question names explicitly; vector matches always score lower.
_NAMED_SCORE: Final = 0.9
FOCUS_CODE_LINES: Final = 140
NEIGHBOUR_CODE_LINES: Final = 30
_MAX_CALLER_FACTS: Final = 8
_MAX_FOOTPRINT_FACTS: Final = 14
_MAX_CALLEE_FACTS: Final = 12


@dataclass(slots=True)
class Fact:
    """One statement derived from the graph, phrased for a reader."""

    #: ``entry`` | ``structure`` | ``caller`` | ``callee`` | ``read`` | ``write`` | ``trigger`` |
    #: ``history`` | ``runtime``
    kind: str
    text: str


@dataclass(slots=True)
class Evidence:
    id: str
    #: ``code`` | ``sql`` | ``table`` | ``commit``
    kind: str
    #: ``focus`` | ``sql`` | ``side-effect`` | ``history`` | ``data`` | ``caller`` | ``callee``
    role: str
    node_id: str
    label: str
    title: str
    #: What an answer should cite: ``PaymentService.java:34`` or ``commit 565bcfe8``.
    citation: str
    text: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    score: float = 0.0
    reason: str = ""
    facts: list[Fact] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "role": self.role,
            "node_id": self.node_id,
            "label": self.label,
            "title": self.title,
            "citation": self.citation,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": round(self.score, 3),
            "reason": self.reason,
            "facts": [{"kind": fact.kind, "text": fact.text} for fact in self.facts],
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass(slots=True)
class ContextPack:
    repository_id: str
    question: str
    evidence: list[Evidence] = field(default_factory=list)
    #: Node ids the retrieval was centred on, best first.
    seeds: list[str] = field(default_factory=list)
    #: Caveats about the retrieval itself (index unavailable, source missing, ...).
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.evidence

    def render(self) -> str:
        """The evidence block placed in the prompt."""
        blocks: list[str] = []
        for item in self.evidence:
            span = ""
            if item.path and item.start_line:
                span = f" · {item.path}:{item.start_line}-{item.end_line or item.start_line}"
            header = f"### [{item.id}] {item.kind} · {item.title}{span} · role: {item.role}"
            parts = [header]
            if item.facts:
                parts.append("Graph facts:\n" + "\n".join(f"- {fact.text}" for fact in item.facts))
            if item.text:
                parts.append(item.text + ("\n… (truncated)" if item.truncated else ""))
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks)


@dataclass(slots=True)
class _Seed:
    node: GraphNode
    score: float
    reason: str


def cite(node: GraphNode, line: int | None = None) -> str:
    """``File.ext:line`` for a code node; the short hash for a commit."""
    if node.label is NodeLabel.COMMIT:
        return f"commit {node.name}"
    if node.path is None:
        return node.name
    number = line or node.line
    base = node.path.rsplit("/", 1)[-1]
    return f"{base}:{number}" if number else base


def _when(node: GraphNode, key: str = "authored_at") -> str:
    return str(node.get(key) or "")[:10]


class GraphRetriever:
    def __init__(
        self,
        store: GraphStore,
        index: VectorIndex | None,
        *,
        max_seeds: int = 4,
        max_evidence: int = 22,
        char_budget: int = 30_000,
        vector_candidates: int = 12,
    ) -> None:
        self.store = store
        self.index = index
        self.max_seeds = max_seeds
        self.max_evidence = max_evidence
        self.char_budget = char_budget
        self.vector_candidates = vector_candidates

    # ------------------------------------------------------------------ seeding

    def _anchors(self, repository_id: str, question: str) -> list[_Seed]:
        """Scopes the question refers to explicitly."""
        seeds: list[_Seed] = []
        for found in locator.LOCATION_RE.finditer(question):
            reference, line = found.group(1), int(found.group(2))
            for location in locator.resolve_location(self.store, repository_id, reference, line)[:2]:
                target = location.scope or location.file
                if target.label is NodeLabel.DATABASE_QUERY and target.get("function_id"):
                    target = self.store.get_node(str(target.get("function_id"))) or target
                seeds.append(_Seed(target, 1.0, f"the question cites {location.citation}"))

        masked = locator.LOCATION_RE.sub(" ", question)
        names = dict.fromkeys(match.strip() for match in _BACKTICK_RE.findall(masked))
        names.update(dict.fromkeys(_IDENTIFIER_RE.findall(masked)))
        for name in list(names)[:12]:
            if len(name) < 3:
                continue
            for node in self._exact(repository_id, name)[:3]:
                seeds.append(_Seed(node, _NAMED_SCORE, f"the question names {name}"))

        for commit_hash in list(dict.fromkeys(_HASH_RE.findall(question)))[:4]:
            for node in self.store.find_nodes(
                repository_id, labels=[NodeLabel.COMMIT], name=commit_hash[:8], limit=2
            ):
                seeds.append(_Seed(node, 0.95, f"the question cites commit {node.name}"))
        return seeds

    def _exact(self, repository_id: str, name: str) -> list[GraphNode]:
        normalised = name.replace("::", ".").replace("#", ".").strip("()")
        labels = [NodeLabel.FUNCTION, NodeLabel.CLASS, NodeLabel.DATABASE_TABLE]
        found = self.store.find_nodes(repository_id, labels=labels, qualified_name=normalised, limit=4)
        if not found:
            found = self.store.find_nodes(
                repository_id, labels=labels, name=normalised.rsplit(".", 1)[-1], limit=6
            )
            if "." in normalised:
                owner = normalised.rsplit(".", 1)[0].lower()
                found = [node for node in found if owner in node.qualified_name.lower()]
        return sorted(
            found, key=lambda node: (bool(node.get("is_test")), node.label is not NodeLabel.CLASS, node.id)
        )

    def _vector_seeds(self, repository_id: str, question: str, pack: ContextPack) -> list[_Seed]:
        if self.index is None:
            pack.notes.append("semantic index not configured: retrieval used names in the question only")
            return []
        try:
            hits = self.index.search(repository_id, question, limit=self.vector_candidates)
        except VectorIndexError as error:
            logger.warning("vector search failed: %s", error)
            pack.notes.append("semantic index unavailable: retrieval used names in the question only")
            return []
        nodes = {node.id: node for node in self.store.get_nodes([hit.node_id for hit in hits])}
        seeds: list[_Seed] = []
        for rank, hit in enumerate(hits):
            node = nodes.get(hit.node_id)
            if node is not None:  # a stale vector whose node is gone is ignored
                seeds.append(_Seed(node, 0.75 - 0.04 * rank, "semantic match with the question"))
        return seeds

    def _to_scopes(self, seed: _Seed) -> list[_Seed]:
        """Map a hit on a query or commit to the code scopes it belongs to."""
        node = seed.node
        if node.label in _SCOPES:
            return [seed]
        if node.label is NodeLabel.DATABASE_QUERY:
            owner = self.store.get_node(str(node.get("function_id") or ""))
            return (
                [_Seed(owner, seed.score * 0.95, f"runs SQL matching the question ({cite(node)})")]
                if owner
                else []
            )
        if node.label is NodeLabel.COMMIT:
            hops = self.store.expand([node.id], [RelType.TOUCHED_BY], Direction.IN)
            touched = [
                hop
                for hop in hops
                if hop.edge.properties.get("scope") == "blame" and hop.node.label is NodeLabel.FUNCTION
            ]
            touched.sort(key=lambda hop: (-int(hop.edge.properties.get("lines") or 0), hop.node.id))
            return [
                _Seed(hop.node, seed.score * 0.85, f"changed by commit {node.name}: {node.get('message')}")
                for hop in touched[:3]
            ]
        if node.label is NodeLabel.FILE:
            members = self.store.expand([node.id], [RelType.CONTAINS], Direction.OUT)
            members.sort(key=lambda hop: (hop.node.label is not NodeLabel.CLASS, hop.node.start_line or 0))
            return [_Seed(hop.node, seed.score * 0.8, f"defined in {node.name}") for hop in members[:2]]
        return []

    # ---------------------------------------------------------------- retrieval

    def retrieve(
        self,
        repository_id: str,
        question: str,
        reader: SourceReader,
        *,
        focus_node_id: str | None = None,
    ) -> ContextPack:
        pack = ContextPack(repository_id, question)
        if not reader.available:
            pack.notes.append("source checkout not available: evidence shows graph facts without code")

        candidates: list[_Seed] = []
        if focus_node_id:
            focus = self.store.get_node(focus_node_id)
            if focus is not None and focus.repository_id == repository_id:
                candidates.append(_Seed(focus, 1.0, "the node currently selected"))
        candidates += self._anchors(repository_id, question)
        candidates += self._vector_seeds(repository_id, question, pack)

        wants_tests = "test" in question.lower()
        commits: dict[str, _Seed] = {}
        best: dict[str, _Seed] = {}
        for candidate in candidates:
            if candidate.node.label is NodeLabel.COMMIT:
                commits.setdefault(candidate.node.id, candidate)
            for seed in self._to_scopes(candidate):
                if seed.node.get("is_test") and not wants_tests:
                    seed.score *= 0.6
                if seed.node.get("is_boilerplate"):
                    seed.score *= 0.7
                if seed.node.get("kind") == "constructor" and seed.score < _NAMED_SCORE:
                    seed.score *= 0.6  # matched on the class's vocabulary, not asked about
                known = best.get(seed.node.id)
                if known is None or seed.score > known.score:
                    best[seed.node.id] = seed
        seeds = sorted(best.values(), key=lambda seed: (-seed.score, seed.node.id))[: self.max_seeds]
        pack.seeds = [seed.node.id for seed in seeds]

        builder = _PackBuilder(self.store, reader, pack)
        for seed in seeds:
            builder.add_seed(seed)
        for commit in sorted(commits.values(), key=lambda seed: -seed.score)[:4]:
            builder.add_commit(commit.node, score=commit.score, reason=commit.reason)
        builder.finish(self.max_evidence, self.char_budget)
        return pack


class _PackBuilder:
    """Accumulates evidence, de-duplicating by node and keeping the strongest role."""

    def __init__(self, store: GraphStore, reader: SourceReader, pack: ContextPack) -> None:
        self.store = store
        self.reader = reader
        self.pack = pack
        self.items: dict[str, Evidence] = {}

    # ------------------------------------------------------------------- adding

    def _put(self, item: Evidence) -> Evidence:
        known = self.items.get(item.node_id)
        if known is None:
            self.items[item.node_id] = item
            return item
        if _ROLE_ORDER[item.role] < _ROLE_ORDER[known.role]:
            item.facts = item.facts or known.facts
            item.score = max(item.score, known.score)
            self.items[item.node_id] = item
            return item
        known.score = max(known.score, item.score)
        return known

    def _code(self, node: GraphNode, *, role: str, score: float, reason: str, max_lines: int) -> Evidence:
        text, truncated = "", False
        start, end = node.start_line, node.end_line
        if node.path and start and end:
            snippet = self.reader.snippet(node.path, start, end, max_lines=max_lines)
            text = numbered(snippet)
            truncated = bool(snippet) and snippet[-1][0] < end
            if snippet:
                end = snippet[-1][0] if truncated else end
        if not text:
            text = "\n".join(part for part in (node.get("signature"), node.get("docstring")) if part)
        kind = "table" if node.label is NodeLabel.DATABASE_TABLE else "code"
        if node.label is NodeLabel.DATABASE_TABLE and not text:
            text = str(node.get("ddl") or "")
        return Evidence(
            id="",
            kind=kind,
            role=role,
            node_id=node.id,
            label=node.label.value,
            title=f"{node.get('kind') or node.label.value.lower()} {node.qualified_name}",
            citation=cite(node),
            text=text,
            path=node.path,
            start_line=start,
            end_line=end,
            score=score,
            reason=reason,
            truncated=truncated,
        )

    def add_commit(self, commit: GraphNode, *, score: float, reason: str) -> Evidence:
        lines = [str(commit.get("message") or "")]
        if commit.get("body"):
            lines.append(str(commit.get("body")))
        if commit.get("pr_title"):
            lines.append(f"Pull request #{commit.get('pr_number')}: {commit.get('pr_title')}")
        if commit.get("pr_body"):
            lines.append(str(commit.get("pr_body"))[:1500])
        pull = f" · PR #{commit.get('pr_number')}" if commit.get("pr_number") else ""
        return self._put(
            Evidence(
                id="",
                kind="commit",
                role="history",
                node_id=commit.id,
                label=commit.label.value,
                title=f"commit {commit.name} · {_when(commit)} · {commit.get('author_name')}{pull}",
                citation=f"commit {commit.name}",
                text="\n".join(lines),
                score=score,
                reason=reason,
            )
        )

    def add_seed(self, seed: _Seed) -> None:
        node = seed.node
        context = node_context(self.store, node.id)
        if context is None:
            return
        item = self._put(
            self._code(node, role="focus", score=seed.score, reason=seed.reason, max_lines=FOCUS_CODE_LINES)
        )
        item.facts = self._facts(context)
        name = node.qualified_name

        for query in context.queries[:6]:
            self._put(self._query(query, score=seed.score * 0.9, reason=f"SQL executed by {name}"))
        if node.label is NodeLabel.FUNCTION:
            item.facts += self._footprint(node, seed.score)
        elif node.label is NodeLabel.DATABASE_TABLE:
            for neighbour in context.triggers:
                self._trigger_evidence(neighbour.node, node.name, seed.score)
        for neighbour in self._history(context)[:4]:
            self.add_commit(neighbour.node, score=seed.score * 0.85, reason=f"history of {name}")

        confident = [n for n in context.callers if n.edge.confidence >= DEFAULT_MIN_CONFIDENCE]
        for neighbour in confident[:3]:
            self._put(
                self._code(
                    neighbour.node,
                    role="caller",
                    score=seed.score * 0.6,
                    reason=f"calls {name}",
                    max_lines=NEIGHBOUR_CODE_LINES,
                )
            )
        callees = [
            n
            for n in context.callees
            if n.edge.confidence >= DEFAULT_MIN_CONFIDENCE and not n.node.get("is_boilerplate")
        ]
        callees.sort(
            key=lambda n: (-int(n.node.get("query_count") or 0), -int(n.node.get("fan_out") or 0), n.node.id)
        )
        for neighbour in callees[:4]:
            self._put(
                self._code(
                    neighbour.node,
                    role="callee",
                    score=seed.score * 0.55,
                    reason=f"called by {name}",
                    max_lines=NEIGHBOUR_CODE_LINES,
                )
            )
        if node.label is NodeLabel.DATABASE_TABLE:
            for neighbour in (context.writers + context.readers)[:5]:
                target = neighbour.node
                if target.label is NodeLabel.DATABASE_QUERY:
                    self._put(self._query(target, score=seed.score * 0.8, reason=f"touches {name}"))
                else:
                    self._put(
                        self._code(
                            target,
                            role="data",
                            score=seed.score * 0.7,
                            reason=f"touches {name}",
                            max_lines=NEIGHBOUR_CODE_LINES,
                        )
                    )

    def _query(self, query: GraphNode, *, score: float, reason: str) -> Evidence:
        # The stored SQL is the statement as executed (string concatenation folded), so its
        # lines do not map one-to-one onto source lines; the span is given in a header instead.
        base = (query.path or "").rsplit("/", 1)[-1]
        span = f"{base}:{query.start_line}"
        if query.end_line and query.end_line != query.start_line:
            span += f"-{query.end_line}"
        notes = [f"-- SQL text located at {span}"]
        if query.get("constant_name"):
            executed = f"{query.get('function_path')}:{query.get('executed_at_line')}"
            notes.append(f"-- defined as constant {query.get('constant_name')}, executed at {executed}")
        if query.get("dynamic"):
            notes.append("-- assembled at runtime; ${...} marks parts that are not literal")
        return Evidence(
            id="",
            kind="sql",
            role="sql",
            node_id=query.id,
            label=query.label.value,
            title=f"{query.name} in {query.get('function_name')}",
            citation=cite(query),
            text="\n".join([*notes, str(query.get("sql") or "")]),
            path=query.path,
            start_line=query.start_line,
            end_line=query.end_line,
            score=score,
            reason=reason,
        )

    # -------------------------------------------------------------------- facts

    def _history(self, context: NodeContext) -> list[Neighbour]:
        """Commits behind the surviving lines: newest first, then by share of lines."""
        blamed = [n for n in context.commits if n.edge.properties.get("scope") == "blame"]
        return blamed or context.commits

    def _facts(self, context: NodeContext) -> list[Fact]:
        node = context.node
        name = node.qualified_name
        facts: list[Fact] = []

        def add(kind: str, text: str) -> None:
            facts.append(Fact(kind, text))

        if node.get("is_entry_point"):
            target = " ".join(str(part) for part in (node.get("http_method"), node.get("route")) if part)
            detail = f" {target}" if target else ""
            add("entry", f"{name} is a {node.get('entry_kind')} entry point{detail} [{cite(node)}]")
        for container in context.containers:
            if container.label is NodeLabel.CLASS:
                kind = container.get("kind") or "class"
                add("structure", f"member of {kind} {container.qualified_name} [{cite(container)}]")
        for neighbour in context.supertypes[:4]:
            relation = neighbour.edge.properties.get("kind") or "extends"
            add("structure", f"{relation} {neighbour.node.qualified_name} [{cite(neighbour.node)}]")
        for neighbour in context.subtypes[:6]:
            add("structure", f"specialised by {neighbour.node.qualified_name} [{cite(neighbour.node)}]")

        if node.get("dead_candidate"):
            add(
                "caller",
                "no caller was found by static analysis (dead-code candidate; reflection, "
                "configuration and callers outside the repository are not visible)",
            )
        for neighbour in context.callers[:_MAX_CALLER_FACTS]:
            add("caller", self._call_fact(neighbour, incoming=True))
        if len(context.callers) > _MAX_CALLER_FACTS:
            add("caller", f"... and {len(context.callers) - _MAX_CALLER_FACTS} more callers")
        for neighbour in context.callees[:_MAX_CALLEE_FACTS]:
            add("callee", self._call_fact(neighbour, incoming=False))
        if len(context.callees) > _MAX_CALLEE_FACTS:
            add("callee", f"... and {len(context.callees) - _MAX_CALLEE_FACTS} more callees")
        unresolved = node.get("unresolved_calls") or []
        if unresolved:
            add(
                "callee",
                "calls outside the repository (libraries / unresolved): " + ", ".join(unresolved[:10]),
            )

        for neighbour in context.reads[:8]:
            add("read", self._data_fact(node, neighbour, "reads"))
        for neighbour in context.writes[:8]:
            add("write", self._data_fact(node, neighbour, "writes"))
        for neighbour in context.writers[:10]:
            add("write", self._table_fact(neighbour, "written"))
        for neighbour in context.readers[:10]:
            add("read", self._table_fact(neighbour, "read"))
        if node.label is NodeLabel.DATABASE_TABLE:
            for neighbour in context.triggers:
                events = "/".join(neighbour.edge.properties.get("events") or [])
                trigger = neighbour.node
                add(
                    "trigger",
                    f"{events} on {node.name} fires trigger {trigger.qualified_name} [{cite(trigger)}]",
                )

        for neighbour in context.observed_callees[:5]:
            count = neighbour.edge.properties.get("count")
            add("runtime", f"observed at runtime calling {neighbour.node.qualified_name} ({count} times)")
        for neighbour in context.observed_callers[:5]:
            count = neighbour.edge.properties.get("count")
            add(
                "runtime",
                f"observed at runtime being called by {neighbour.node.qualified_name} ({count} times)",
            )

        history = self._history(context)
        if history:
            newest, oldest = history[0].node, history[-1].node
            add("history", f"last changed {self._commit_phrase(newest)}")
            if oldest.id != newest.id:
                add("history", f"oldest surviving lines from {self._commit_phrase(oldest)}")
            authors = node.get("authors") or []
            if authors:
                add("history", "line ownership: " + ", ".join(authors))
        return facts

    @staticmethod
    def _commit_phrase(commit: GraphNode) -> str:
        author = commit.get("author_name")
        return f"{_when(commit)} by {author} in [commit {commit.name}]: {commit.get('message')}"

    @staticmethod
    def _call_fact(neighbour: Neighbour, *, incoming: bool) -> str:
        other, edge = neighbour.node, neighbour.edge
        lines = edge.properties.get("lines") or []
        if incoming:
            where = cite(other, lines[0] if lines else None)
            text = f"called by {other.qualified_name} at [{where}]"
        else:
            text = f"calls {other.qualified_name} [{cite(other)}]" + (
                f" (call at line {lines[0]})" if lines else ""
            )
        if edge.confidence < DEFAULT_MIN_CONFIDENCE:
            text += f" - UNCERTAIN, name match only (confidence {edge.confidence:.2f})"
        elif edge.properties.get("resolution") in ("implementation", "subclass"):
            text += " - via dynamic dispatch (one possible implementation)"
        return text

    @staticmethod
    def _data_fact(node: GraphNode, neighbour: Neighbour, verb: str) -> str:
        edge = neighbour.edge
        operations = "/".join(edge.properties.get("operations") or [])
        lines = edge.properties.get("lines") or []
        where = f" at [{cite(node, lines[0])}]" if lines and node.path else ""
        text = f"{verb} table {neighbour.node.name} ({operations}){where}"
        if edge.properties.get("via") == "name_reference":
            text = f"mentions table {neighbour.node.name} by name{where} - UNCERTAIN, no SQL statement found"
        return text

    @staticmethod
    def _table_fact(neighbour: Neighbour, verb: str) -> str:
        other, edge = neighbour.node, neighbour.edge
        operations = "/".join(edge.properties.get("operations") or [])
        lines = edge.properties.get("lines") or []
        owner = other.get("function_name") or other.qualified_name
        return f"{verb} by {owner} ({operations}) [{cite(other, lines[0] if lines else None)}]"

    def _trigger_evidence(self, trigger: GraphNode, table: str, score: float) -> None:
        """Show what a trigger does: its own code and that of the routine it delegates to."""
        scopes = [trigger]
        effects = node_context(self.store, trigger.id)
        if effects is not None:
            scopes += [neighbour.node for neighbour in effects.callees[:3]]
        for scope in scopes:
            self._put(
                self._code(
                    scope,
                    role="side-effect",
                    score=score * 0.8,
                    reason=f"runs when {table} is written",
                    max_lines=NEIGHBOUR_CODE_LINES + 10,
                )
            )

    def _footprint(self, node: GraphNode, score: float) -> list[Fact]:
        """What running ``node`` ultimately touches, and where execution can come from.

        Follows calls downstream into tables and the triggers they fire, so effects
        that are invisible in the function's own code - a stored procedure three
        calls away, an audit trigger - become explicit facts.
        """
        facts: list[Fact] = []
        down = self.store.traverse(
            [node.id],
            [RelType.CALLS, RelType.READS_FROM_DB, RelType.MUTATES_TABLE, RelType.FIRES_TRIGGER],
            direction=Direction.OUT,
            max_hops=6,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
            max_nodes=300,
            node_filter=lambda found: found.label in (NodeLabel.FUNCTION, NodeLabel.DATABASE_TABLE),
        )
        triggered: set[str] = set()  # functions that only run because a trigger fired
        fired = [edge for edge in down.edges.values() if edge.type is RelType.FIRES_TRIGGER]
        frontier = [edge.target for edge in fired]
        while frontier:
            current = frontier.pop()
            if current in triggered:
                continue
            triggered.add(current)
            frontier += [edge.target for edge in down.out_edges(current, RelType.CALLS)]

        accesses = [
            edge
            for edge in down.edges.values()
            if edge.type in (RelType.READS_FROM_DB, RelType.MUTATES_TABLE) and edge.source != node.id
        ]
        accesses.sort(key=lambda edge: (down.depths.get(edge.source, 9), edge.type.value, edge.target))
        for edge in accesses[:_MAX_FOOTPRINT_FACTS]:
            actor, table = down.nodes[edge.source], down.nodes[edge.target]
            write = edge.type is RelType.MUTATES_TABLE
            operations = "/".join(edge.properties.get("operations") or [])
            lines = edge.properties.get("lines") or []
            where = cite(actor, lines[0] if lines else None)
            origin = "inside a database trigger" if actor.id in triggered else "downstream"
            verb = "writes" if write else "reads"
            facts.append(
                Fact(
                    "write" if write else "read",
                    f"{origin}: {actor.qualified_name} {verb} table {table.name} ({operations}) [{where}]",
                )
            )
        for edge in fired:
            table, trigger = down.nodes[edge.source], down.nodes[edge.target]
            events = "/".join(edge.properties.get("events") or [])
            facts.append(
                Fact(
                    "trigger",
                    f"HIDDEN SIDE EFFECT: a write to {table.name} fires database trigger "
                    f"{trigger.qualified_name} on {events} [{cite(trigger)}]",
                )
            )
            self._trigger_evidence(trigger, table.name, score)
        if down.truncated:
            facts.append(Fact("write", "the downstream call graph was truncated; more tables may be touched"))

        # SQL run by downstream functions is the most concrete evidence of a data effect.
        writers = list(dict.fromkeys(edge.source for edge in accesses if edge.type is RelType.MUTATES_TABLE))
        if writers:
            hops = self.store.expand(writers[:6], [RelType.CONTAINS], Direction.OUT)
            queries = [hop.node for hop in hops if hop.node.label is NodeLabel.DATABASE_QUERY]
            queries.sort(
                key=lambda query: (not query.get("tables_written"), query.path or "", query.start_line or 0)
            )
            for query in queries[:5]:
                owner = query.get("function_name")
                self._put(self._query(query, score=score * 0.75, reason=f"run downstream by {owner}"))

        up = self.store.traverse(
            [node.id],
            [RelType.CALLS],
            direction=Direction.IN,
            max_hops=5,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
            max_nodes=200,
        )
        entries = [
            found for found in up.nodes.values() if found.get("is_entry_point") and found.id != node.id
        ]
        entries.sort(key=lambda found: (up.depths.get(found.id, 9), found.qualified_name))
        for entry in entries[:5]:
            target = " ".join(str(part) for part in (entry.get("http_method"), entry.get("route")) if part)
            label = f"{entry.get('entry_kind')} entry point {target}".strip()
            facts.append(Fact("entry", f"reachable from {label} ({entry.qualified_name}) [{cite(entry)}]"))
        return facts

    # ------------------------------------------------------------------- finish

    def finish(self, max_evidence: int, char_budget: int) -> None:
        ordered = sorted(
            self.items.values(), key=lambda item: (_ROLE_ORDER[item.role], -item.score, item.node_id)
        )
        used = 0
        kept: list[Evidence] = []
        for item in ordered:
            size = len(item.text) + sum(len(fact.text) for fact in item.facts) + 120
            if kept and (len(kept) >= max_evidence or used + size > char_budget):
                continue
            used += size
            kept.append(item)
        for number, item in enumerate(kept, start=1):
            item.id = f"E{number}"
        self.pack.evidence = kept
