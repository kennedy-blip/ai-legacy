"""Deterministic answer assembled from graph facts, used when no language model is configured.

This is not a degraded imitation of an LLM answer: it is a structured read-out of
what static analysis and git history established about the scopes most relevant
to the question. It cannot interpret or summarise prose, and says so.
"""

from __future__ import annotations

from rag_engine.retrieval import ContextPack, Evidence

NOTICE = (
    "_No language model is configured, so this is a structured report of graph facts rather than "
    "a written explanation. Set `ARCHAEO_LLM_PROVIDER` to enable generated answers._"
)

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("entry", "How execution gets here"),
    ("caller", "Called by"),
    ("callee", "Calls"),
    ("read", "Reads"),
    ("write", "Writes"),
    ("trigger", "Hidden side effects"),
    ("runtime", "Runtime"),
    ("history", "History"),
    ("structure", "Structure"),
)


def _headline(item: Evidence) -> str:
    span = f" [{item.citation}]" if item.citation else ""
    return f"### `{item.title}`{span}"


def _first_paragraph(text: str, limit: int = 400) -> str:
    paragraph = text.strip().split("\n\n", 1)[0].strip()
    return paragraph if len(paragraph) <= limit else paragraph[: limit - 1].rstrip() + "…"


def build_report(pack: ContextPack) -> str:
    """Markdown report for the evidence in ``pack``."""
    if pack.is_empty:
        return (
            "Nothing in the knowledge graph matched this question. Try naming a class, function, table or a "
            "`File.ext:line` location, or check that the repository finished ingesting."
        )
    focus = [item for item in pack.evidence if item.role == "focus"]
    commits = [item for item in pack.evidence if item.kind == "commit"]
    lines: list[str] = [NOTICE, ""]
    lines.append(
        "**Most relevant scopes:** " + ", ".join(f"`{item.title}` [{item.citation}]" for item in focus[:4])
    )
    for item in focus[:3]:
        lines += ["", _headline(item)]
        if item.reason:
            lines.append(f"_Selected because {item.reason}._")
        for kind, title in _SECTIONS:
            facts = [fact.text for fact in item.facts if fact.kind == kind]
            if facts:
                lines += ["", f"**{title}**"]
                lines += [f"- {fact}" for fact in facts]
    if commits:
        lines += ["", "### Commits behind this code"]
        for item in commits[:6]:
            lines.append(
                f"- [{item.citation}] {item.title.split(' · ', 1)[-1]} - {_first_paragraph(item.text)}"
            )
    if pack.notes:
        lines += ["", "### Retrieval notes"] + [f"- {note}" for note in pack.notes]
    return "\n".join(lines)
