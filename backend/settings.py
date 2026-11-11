"""Runtime configuration, read from ``ARCHAEO_*`` environment variables (and an optional ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from rag_engine.llm import LlmConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def _split(value: Any) -> Any:
    """Accept ``a,b,c`` for list settings (JSON arrays are also accepted)."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json  # noqa: PLC0415

            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHAEO_", env_file=".env", extra="ignore")

    environment: Literal["development", "production"] = "development"
    #: Embedded stores, cloned repositories and the demo repository live here.
    data_dir: Path = Path("data")
    log_level: str = "INFO"

    # ---- knowledge graph
    graph_backend: Literal["sqlite", "neo4j"] = "sqlite"
    sqlite_path: Path | None = None
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"

    # ---- vector index
    vector_enabled: bool = True
    #: ``memory``: in-process index rebuilt from the graph at start-up (no extra package).
    #: ``qdrant``: Qdrant, embedded under ``data_dir`` or the server at ``qdrant_url``.
    #: ``auto``: Qdrant when ``qdrant_url`` is set or the qdrant-client package is installed.
    vector_backend: Literal["auto", "memory", "qdrant"] = "auto"
    #: URL of a Qdrant server. Empty = embedded Qdrant under ``data_dir`` (single process only).
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    embedding_provider: Literal["hashing", "openai"] = "hashing"
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_dimension: int | None = Field(default=None, ge=32, le=8192)

    # ---- language model
    llm_provider: Literal["none", "openai", "anthropic", "ollama"] = "none"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1500, ge=64, le=16000)
    llm_timeout: float = 120.0

    # ---- jobs
    job_backend: Literal["inline", "celery"] = "inline"
    redis_url: str = "redis://localhost:6379/0"

    # ---- ingestion
    workspace_dir: Path | None = None
    #: Directories from which local (non-cloned) repositories may be ingested.
    allowed_local_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    #: Hosts repositories may be cloned from, e.g. ``github.com,git.corp.example``. Empty = any.
    allowed_git_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_files: int = Field(default=20_000, ge=1)
    max_file_bytes: int = Field(default=1_500_000, ge=1024)
    max_commits: int = Field(default=20_000, ge=0)
    blame: bool = True
    blame_budget_seconds: float = 300.0
    github_enrichment: bool = True
    github_token: SecretStr | None = None
    #: Add the synthetic demo repository when the graph is empty at start-up.
    seed_demo: bool = True
    #: ``ingest``: build the demo git repository and run the real pipeline on it (needs git).
    #: ``snapshot``: load the pre-computed graph shipped in ``examples/demo-monolith``.
    #: ``auto``: ingest, falling back to the snapshot where git is unavailable.
    demo_source: Literal["auto", "ingest", "snapshot"] = "auto"

    # ---- API
    #: When set, every API request must carry ``Authorization: Bearer <token>``.
    api_token: SecretStr | None = None
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    telemetry_enabled: bool = True
    #: Serve existing graphs only: ingestion, deletion and telemetry intake are refused.
    #: For hosts without git, workers or durable storage (see ``backend.vercel_app``).
    read_only: bool = False

    @field_validator("allowed_local_roots", "allowed_git_hosts", "cors_origins", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return _split(value)

    @field_validator(
        "sqlite_path",
        "workspace_dir",
        "qdrant_url",
        "qdrant_api_key",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_dimension",
        "llm_api_key",
        "llm_base_url",
        "github_token",
        "api_token",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        """``ARCHAEO_API_TOKEN=`` in an env file means "not set", not an empty token."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.job_backend == "celery" and self.vector_enabled and not self.qdrant_url:
            raise ValueError(
                "ARCHAEO_JOB_BACKEND=celery needs a Qdrant server (ARCHAEO_QDRANT_URL): the embedded and "
                "in-process indexes belong to one process, and workers run in their own process"
            )
        if self.vector_backend == "memory" and self.qdrant_url:
            raise ValueError("ARCHAEO_VECTOR_BACKEND=memory conflicts with ARCHAEO_QDRANT_URL")
        if (
            self.job_backend == "celery"
            and self.graph_backend == "sqlite"
            and self.environment == "production"
        ):
            raise ValueError("ARCHAEO_JOB_BACKEND=celery in production requires ARCHAEO_GRAPH_BACKEND=neo4j")
        if self.environment == "production" and self.api_token is None:
            raise ValueError("ARCHAEO_API_TOKEN must be set when ARCHAEO_ENVIRONMENT=production")
        return self

    # ------------------------------------------------------------ derived paths

    @property
    def graph_path(self) -> Path:
        return self.sqlite_path or self.data_dir / "graph.sqlite3"

    @property
    def workspace_path(self) -> Path:
        return self.workspace_dir or self.data_dir / "repos"

    @property
    def qdrant_location(self) -> str:
        return self.qdrant_url or str(self.data_dir / "qdrant")

    @property
    def demo_path(self) -> Path:
        return self.data_dir / "demo" / "core-billing-monolith"

    @property
    def local_roots(self) -> tuple[Path, ...]:
        """Configured roots plus the directory holding the generated demo repository."""
        return (*self.allowed_local_roots, self.demo_path.parent)

    def llm_config(self) -> LlmConfig:
        return LlmConfig(
            provider=self.llm_provider,
            model=self.llm_model,
            api_key=self.llm_api_key.get_secret_value() if self.llm_api_key else None,
            base_url=self.llm_base_url,
            temperature=self.llm_temperature,
            timeout=self.llm_timeout,
            max_tokens=self.llm_max_tokens,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
