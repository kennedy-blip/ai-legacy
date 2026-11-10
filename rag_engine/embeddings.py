"""Text embedding backends.

Two kinds of vectors are produced for every indexed scope:

* a **dense** vector from the configured :class:`Embedder`;
* a **sparse** lexical vector (:func:`sparse_vector`) that Qdrant weights with IDF.
  Identifiers are split into sub-words, so ``applyPayment`` matches a question
  about "apply payment" and ``tbl_ledger_audit`` matches "ledger audit".

The default dense backend, :class:`HashingEmbedder`, needs no model download and
no network: it hashes word and sub-word features into a fixed-size vector. It is
a *lexical* embedding - it captures shared vocabulary, not meaning - and exists so
the platform works offline out of the box. Configure an OpenAI-compatible
embedding endpoint for true semantic retrieval.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
from typing import Any, Final, Protocol

_WORD_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_CAMEL_RE: Final = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP_WORDS: Final = frozenset(
    """
    a an and are as at be but by do does for from had has have how i if in into is it its of on or that the
    their then there these this to was were what when where which who why will with would you your can could
    should not no yes we they he she them his her our us am been being did than so such via per
    public private protected static final void return new class def function var let const import package
    throws throw try catch else null none true false self
    """.split()  # noqa: SIM905
)
_SPARSE_SPACE: Final = 2**31 - 1


def tokenize(text: str) -> list[str]:
    """Lower-cased words plus the sub-words of compound identifiers.

    ``applyPayment`` -> ``applypayment``, ``apply``, ``payment``;
    ``tbl_ledger_audit`` -> ``tbl_ledger_audit``, ``tbl``, ``ledger``, ``audit``.
    """
    tokens: list[str] = []
    for word in _WORD_RE.findall(text):
        lowered = word.lower()
        if len(lowered) < 2 and not lowered.isdigit():
            continue
        if lowered not in _STOP_WORDS:
            tokens.append(lowered)
        parts = [part.lower() for chunk in word.split("_") for part in _CAMEL_RE.findall(chunk)]
        if len(parts) > 1:
            tokens.extend(part for part in parts if len(part) > 1 and part not in _STOP_WORDS)
    return tokens


def _stem(token: str) -> str:
    """Crude suffix stripping so ``payments``/``payment`` and ``reconciling``/``reconcile`` meet."""
    for suffix, minimum in (
        ("ations", 5),
        ("ation", 5),
        ("ings", 4),
        ("ing", 4),
        ("ies", 3),
        ("es", 4),
        ("ed", 4),
        ("s", 3),
    ):
        if token.endswith(suffix) and len(token) - len(suffix) >= minimum:
            return token[: -len(suffix)]
    return token.removesuffix("e") if len(token) > 4 else token


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def sparse_vector(text: str, *, query: bool = False) -> tuple[list[int], list[float]]:
    """Sparse lexical vector as ``(indices, values)`` with sorted, unique indices.

    Documents carry a dampened term frequency; queries carry 1.0 per term. The
    IDF part of the weighting is applied by the vector database.
    """
    counts = Counter(_stem(token) for token in tokenize(text))
    weights: dict[int, float] = {}
    for token, count in counts.items():
        index = _hash(token) % _SPARSE_SPACE
        weight = 1.0 if query else 1.0 + math.log(count)
        weights[index] = max(weights.get(index, 0.0), weight)
    indices = sorted(weights)
    return indices, [weights[index] for index in indices]


class Embedder(Protocol):
    """Produces dense vectors. Implementations must be safe to call from several threads."""

    #: Identifies the vector space; vectors of different embedders are never mixed.
    name: str

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic, offline, lexical embedding by signed feature hashing."""

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 32:
            raise ValueError("dimension must be at least 32")
        self._dimension = dimension
        self.name = f"hashing-{dimension}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        tokens = [_stem(token) for token in tokenize(text)]
        counts = Counter(tokens)
        counts.update(f"{first} {second}" for first, second in pairwise(tokens))
        for feature, count in counts.items():
            digest = _hash(feature)
            sign = 1.0 if digest & 1 else -1.0
            vector[(digest >> 1) % self._dimension] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0  # an all-zero vector has no cosine similarity
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OpenAICompatibleEmbedder:
    """Embeddings from any endpoint speaking the OpenAI ``/embeddings`` protocol.

    Works with OpenAI, Azure-style gateways, Ollama, vLLM, LM Studio, TEI and similar.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        dimension: int | None = None,
        request_dimensions: bool = False,
        timeout: float = 60.0,
    ) -> None:
        from langchain_openai import OpenAIEmbeddings  # noqa: PLC0415 - optional at import time

        options: dict[str, Any] = {
            "model": model,
            "api_key": api_key or "not-needed",
            "timeout": timeout,
            "max_retries": 2,
            # Non-OpenAI servers do not accept pre-tokenised input.
            "check_embedding_ctx_length": base_url is None,
        }
        if base_url:
            options["base_url"] = base_url
        if request_dimensions and dimension:
            options["dimensions"] = dimension
        self._client = OpenAIEmbeddings(**options)
        self._dimension = dimension
        self.name = f"{model}@{base_url or 'openai'}"

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._dimension = len(self._client.embed_query("dimension probe"))
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._client.embed_documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)
