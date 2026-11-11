"""Verification of the citations in a generated answer.

A grounded answer is only as trustworthy as its citations, so every
``[File.ext:123]`` and ``[commit abc1234]`` is checked after generation:

``verified``
    The location lies inside a piece of evidence that was given to the model (or
    is one of the graph facts in it). The claim can be traced to its source.
``outside-context``
    The location exists in the repository but was *not* part of the evidence. The
    model cannot have read it, so the claim attached to it is not grounded.
``unresolved``
    No such file/line or commit exists. The citation is fabricated.

The API returns these verdicts with the answer; the UI renders them differently
so a reader never has to take a citation on faith.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from graph_engine import locator
from graph_engine.schema import NodeLabel
from graph_engine.store import GraphStore
from rag_engine.retrieval import ContextPack, Evidence

Status = Literal["verified", "outside-context", "unresolved"]

_BRACKET_RE: Final = re.compile(r"\[([^\[\]\n]{3,200})\]")
_COMMIT_RE: Final = re.compile(r"^commit\s+([0-9a-f]{7,40})$", re.IGNORECASE)
_MIN_HASH: Final = 7


@dataclass(slots=True)
class Citation:
    #: The citation exactly as written, without brackets.
    raw: str
    kind: Literal["code", "commit"]
    status: Status
    path: str | None = None
    line: int | None = None
    end_line: int | None = None
    commit: str | None = None
    #: Graph node to open when the citation is clicked.
    node_id: str | None = None
    evidence_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "status": self.status,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "commit": self.commit,
            "node_id": self.node_id,
            "evidence_id": self.evidence_id,
            "detail": self.detail,
        }


@dataclass(slots=True)
class CitationReport:
    citations: list[Citation] = field(default_factory=list)

    def count(self, status: Status) -> int:
        return sum(1 for citation in self.citations if citation.status == status)

    @property
    def grounded(self) -> bool:
        """At least one citation, and none that is fabricated or outside the evidence."""
        return bool(self.citations) and self.count("verified") == len(self.citations)

    def summary(self) -> dict[str, Any]:
        return {
            "total": len(self.citations),
            "verified": self.count("verified"),
            "outside_context": self.count("outside-context"),
            "unresolved": self.count("unresolved"),
            "grounded": self.grounded,
        }


def extract(answer: str) -> list[tuple[str, tuple[str, int, int | None] | str]]:
    """Citations in ``answer`` in order of first appearance, without duplicates.

    Returns ``(raw text, parsed)`` where ``parsed`` is ``(file, line, end line)``
    for a code citation or the hash for a commit citation. One bracket may hold
    several citations separated by commas or semicolons.
    """
    found: dict[str, tuple[str, int, int | None] | str] = {}
    for bracket in _BRACKET_RE.findall(answer):
        for part in re.split(r"[;,]\s*", bracket):
            raw = part.strip().strip("`")
            if not raw or raw in found:
                continue
            commit = _COMMIT_RE.match(raw)
            if commit:
                found[raw] = commit.group(1).lower()
                continue
            location = locator.parse_location(raw)
            if location is not None:
                found[raw] = location
    return list(found.items())


def _matches_file(evidence_path: str, cited: str) -> bool:
    return evidence_path == cited or ("/" + evidence_path).endswith("/" + cited.lstrip("./"))


def _evidence_for(pack: ContextPack, cited: str, line: int) -> Evidence | None:
    """The tightest evidence span containing ``cited:line``."""
    best: Evidence | None = None
    for item in pack.evidence:
        if item.path is None or item.start_line is None or not _matches_file(item.path, cited):
            continue
        end = item.end_line or item.start_line
        if item.start_line <= line <= end and (
            best is None or end - item.start_line < (best.end_line or 0) - (best.start_line or 0)
        ):
            best = item
    return best


def _fact_citations(pack: ContextPack) -> tuple[set[tuple[str, int]], set[str]]:
    """Locations and commits that appear in graph facts handed to the model."""
    locations: set[tuple[str, int]] = set()
    commits: set[str] = set()
    for item in pack.evidence:
        for fact in item.facts:
            for _, parsed in extract(fact.text):
                if isinstance(parsed, str):
                    commits.add(parsed)
                else:
                    locations.add((parsed[0].rsplit("/", 1)[-1], parsed[1]))
    return locations, commits


def verify(answer: str, pack: ContextPack, store: GraphStore) -> CitationReport:
    """Check every citation in ``answer`` against the evidence and the graph."""
    report = CitationReport()
    fact_locations, fact_commits = _fact_citations(pack)
    shown_commits = {item.citation.split()[-1]: item for item in pack.evidence if item.kind == "commit"}

    for raw, parsed in extract(answer):
        if isinstance(parsed, str):
            report.citations.append(_verify_commit(raw, parsed, shown_commits, fact_commits, pack, store))
            continue
        cited, line, end_line = parsed
        citation = Citation(raw, "code", "unresolved", path=cited, line=line, end_line=end_line)
        evidence = _evidence_for(pack, cited, line)
        if evidence is not None:
            citation.status = "verified"
            citation.path, citation.node_id, citation.evidence_id = (
                evidence.path,
                evidence.node_id,
                evidence.id,
            )
        else:
            locations = locator.resolve_location(store, pack.repository_id, cited, line, end_line)
            if locations:
                location = locations[0]
                citation.path = location.path
                citation.node_id = (location.scope or location.file).id
                if (cited.rsplit("/", 1)[-1], line) in fact_locations:
                    citation.status = "verified"
                    citation.detail = "cited from a graph fact"
                else:
                    citation.status = "outside-context"
                    citation.detail = "exists in the repository but was not part of the retrieved evidence"
            else:
                citation.detail = "no such file and line in the repository"
        report.citations.append(citation)
    return report


def _verify_commit(
    raw: str,
    commit_hash: str,
    shown: dict[str, Evidence],
    fact_commits: set[str],
    pack: ContextPack,
    store: GraphStore,
) -> Citation:
    citation = Citation(raw, "commit", "unresolved", commit=commit_hash)

    def same(known: str) -> bool:
        return len(known) >= _MIN_HASH and (known.startswith(commit_hash) or commit_hash.startswith(known))

    for known, evidence in shown.items():
        if same(known):
            citation.status, citation.node_id, citation.evidence_id = (
                "verified",
                evidence.node_id,
                evidence.id,
            )
            return citation
    nodes = store.search_nodes(pack.repository_id, commit_hash[:8], labels=[NodeLabel.COMMIT], limit=3)
    nodes = [node for node in nodes if str(node.get("hash") or "").startswith(commit_hash)]
    if len(nodes) == 1:
        citation.node_id = nodes[0].id
        if any(same(known) for known in fact_commits):
            citation.status, citation.detail = "verified", "cited from a graph fact"
        else:
            citation.status = "outside-context"
            citation.detail = "exists in the repository history but was not part of the retrieved evidence"
    else:
        citation.detail = "no such commit in the ingested history"
    return citation
