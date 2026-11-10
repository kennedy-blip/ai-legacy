"""Question answering over one repository: retrieve, generate, verify."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graph_engine.schema import NodeLabel, make_id
from graph_engine.store import GraphStore
from rag_engine.citations import verify
from rag_engine.prompts import explain_prompt
from rag_engine.report import build_report
from rag_engine.retrieval import ContextPack, GraphRetriever
from rag_engine.source import SourceReader
from rag_engine.vector_index import VectorIndex

if TYPE_CHECKING:  # LangChain is loaded only when a language model is configured
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import Runnable

logger = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS = 2000


class UnknownRepositoryError(LookupError):
    """The repository id is not present in the graph."""


@dataclass(slots=True)
class ChatTurn:
    role: str  # "user" | "assistant"
    content: str


class RagEngine:
    def __init__(
        self,
        store: GraphStore,
        index: VectorIndex | None,
        *,
        llm: BaseChatModel | None = None,
        llm_label: str | None = None,
        retriever: GraphRetriever | None = None,
    ) -> None:
        self.store = store
        self.index = index
        self.llm = llm
        self.llm_label = llm_label if llm is not None else None
        self.retriever = retriever or GraphRetriever(store, index)
        self._chain: Runnable[dict[str, Any], str] | None = None
        if llm is not None:
            from langchain_core.output_parsers import StrOutputParser  # noqa: PLC0415

            self._chain = explain_prompt() | llm | StrOutputParser()

    @property
    def mode(self) -> str:
        return "llm" if self._chain is not None else "graph-report"

    # ------------------------------------------------------------------ helpers

    def repository(self, repository_id: str) -> tuple[str, SourceReader]:
        """Display name and source reader of a repository."""
        node = self.store.get_node(make_id(NodeLabel.REPOSITORY, repository_id))
        if node is None:
            raise UnknownRepositoryError(repository_id)
        return node.name, SourceReader(node.get("workspace_path"))

    def retrieve(self, repository_id: str, question: str, *, focus_node_id: str | None = None) -> ContextPack:
        _, reader = self.repository(repository_id)
        return self.retriever.retrieve(repository_id, question, reader, focus_node_id=focus_node_id)

    @staticmethod
    def _history(turns: Sequence[ChatTurn]) -> list[BaseMessage]:
        from langchain_core.messages import AIMessage, HumanMessage  # noqa: PLC0415

        messages: list[BaseMessage] = []
        for turn in list(turns)[-_MAX_HISTORY_TURNS:]:
            content = turn.content[:_MAX_HISTORY_CHARS]
            messages.append(HumanMessage(content) if turn.role == "user" else AIMessage(content))
        return messages

    # ------------------------------------------------------------------ answers

    def stream(
        self,
        repository_id: str,
        question: str,
        *,
        focus_node_id: str | None = None,
        history: Sequence[ChatTurn] = (),
    ) -> Iterator[dict[str, Any]]:
        """Answer ``question`` as a stream of events.

        Event types, in order: one ``context`` (the evidence, before any text), any
        number of ``token``, optionally ``warning``, one ``citations`` (verification
        verdicts) and a final ``done`` carrying the complete answer.
        """
        name, reader = self.repository(repository_id)
        pack = self.retriever.retrieve(repository_id, question, reader, focus_node_id=focus_node_id)
        use_llm = self._chain is not None and not pack.is_empty
        mode = "llm" if use_llm else "graph-report"
        yield {
            "type": "context",
            "mode": mode,
            "model": self.llm_label if use_llm else None,
            "seeds": pack.seeds,
            "notes": pack.notes,
            "evidence": [item.to_dict() for item in pack.evidence],
        }

        warnings: list[str] = []
        parts: list[str] = []
        if use_llm and self._chain is not None:
            inputs = {
                "repository": name,
                "context": pack.render(),
                "question": question,
                "history": self._history(history),
            }
            try:
                for chunk in self._chain.stream(inputs):
                    if chunk:
                        parts.append(chunk)
                        yield {"type": "token", "text": chunk}
            except Exception as error:  # provider outage, bad key, timeout: degrade, do not fail the request
                logger.exception("language model call failed")
                warning = (
                    f"The language model call failed ({error.__class__.__name__}); showing the graph report."
                )
                warnings.append(warning)
                yield {"type": "warning", "message": warning}
                mode = "graph-report"
                separator = "\n\n---\n\n" if parts else ""
                fallback = separator + build_report(pack)
                parts.append(fallback)
                yield {"type": "token", "text": fallback}
        else:
            report = build_report(pack)
            parts.append(report)
            yield {"type": "token", "text": report}

        text = "".join(parts)
        citations = verify(text, pack, self.store)
        if mode == "llm" and not citations.citations:
            warnings.append("The answer contains no citations, so none of its claims could be verified.")
        elif citations.count("unresolved"):
            unresolved = citations.count("unresolved")
            warnings.append(
                f"{unresolved} citation(s) point to locations that do not exist in the repository."
            )
        yield {
            "type": "citations",
            "citations": [citation.to_dict() for citation in citations.citations],
            "summary": citations.summary(),
        }
        yield {
            "type": "done",
            "answer": text,
            "mode": mode,
            "model": self.llm_label if mode == "llm" else None,
            "warnings": warnings,
        }

    def answer(
        self,
        repository_id: str,
        question: str,
        *,
        focus_node_id: str | None = None,
        history: Sequence[ChatTurn] = (),
    ) -> dict[str, Any]:
        """Non-streaming variant of :meth:`stream`: the merged result as one payload."""
        result: dict[str, Any] = {}
        for event in self.stream(repository_id, question, focus_node_id=focus_node_id, history=history):
            kind = event["type"]
            if kind == "context":
                result.update({key: event[key] for key in ("seeds", "notes", "evidence")})
            elif kind == "citations":
                result.update({"citations": event["citations"], "citation_summary": event["summary"]})
            elif kind == "done":
                result.update({key: event[key] for key in ("answer", "mode", "model", "warnings")})
        return result
