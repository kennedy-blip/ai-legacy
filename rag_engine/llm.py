"""Chat-model construction. Providers are imported lazily so only the one in use must be installed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

Provider = Literal["none", "openai", "anthropic", "ollama"]


class LlmConfigurationError(RuntimeError):
    """The configured language model cannot be constructed."""


@dataclass(slots=True)
class LlmConfig:
    provider: Provider = "none"
    model: str = ""
    api_key: str | None = field(default=None, repr=False)
    #: Any OpenAI-compatible endpoint (Azure gateway, vLLM, LM Studio, Ollama ``/v1``).
    base_url: str | None = None
    temperature: float = 0.0
    timeout: float = 120.0
    max_tokens: int | None = 1500

    @property
    def label(self) -> str | None:
        return f"{self.provider}:{self.model}" if self.provider != "none" else None


def build_chat_model(config: LlmConfig) -> BaseChatModel | None:
    """The configured chat model, or ``None`` when answers come from the graph report alone."""
    if config.provider == "none":
        return None
    if not config.model:
        raise LlmConfigurationError("ARCHAEO_LLM_MODEL must be set when an LLM provider is configured")
    options: dict[str, Any] = {
        "model": config.model,
        "temperature": config.temperature,
        "timeout": config.timeout,
        "max_retries": 2,
    }
    if config.max_tokens:
        options["max_tokens"] = config.max_tokens

    if config.provider in ("openai", "ollama"):
        from langchain_openai import ChatOpenAI  # noqa: PLC0415

        base_url = config.base_url
        if config.provider == "ollama":
            base_url = base_url or "http://localhost:11434/v1"
        elif not config.api_key and not base_url:
            raise LlmConfigurationError("ARCHAEO_LLM_API_KEY is required for the openai provider")
        if base_url:
            options["base_url"] = base_url
        return ChatOpenAI(api_key=SecretStr(config.api_key or "not-needed"), **options)

    if config.provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # noqa: PLC0415
        except ImportError as error:
            raise LlmConfigurationError(
                "the anthropic provider needs the optional dependency: pip install 'archaeo-code[anthropic]'"
            ) from error
        if not config.api_key:
            raise LlmConfigurationError("ARCHAEO_LLM_API_KEY is required for the anthropic provider")
        chat: BaseChatModel = ChatAnthropic(api_key=SecretStr(config.api_key), **options)
        return chat

    raise LlmConfigurationError(f"unknown LLM provider: {config.provider!r}")
