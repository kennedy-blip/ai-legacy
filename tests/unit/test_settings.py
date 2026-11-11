"""Settings, and the example file that documents them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.settings import REPO_ROOT, Settings

EXAMPLE = REPO_ROOT / ".env.example"
_LINE = re.compile(r"#(ARCHAEO_[A-Z0-9_]+)=(\S*)")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in [name for name in os.environ if name.startswith("ARCHAEO_")]:
        monkeypatch.delenv(name)


def _documented() -> dict[str, str]:
    return {
        found.group(1): found.group(2)
        for line in EXAMPLE.read_text().splitlines()
        if (found := _LINE.match(line))
    }


def test_example_lists_every_setting_and_nothing_else() -> None:
    names = {f"ARCHAEO_{name.upper()}" for name in Settings.model_fields}
    assert set(_documented()) == names


def test_example_switched_on_gives_the_defaults(tmp_path: Path) -> None:
    """Uncommenting any line of the example must not change behaviour."""
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(line[1:] if _LINE.match(line) else line for line in EXAMPLE.read_text().splitlines())
    )
    loaded, defaults = Settings(_env_file=env), Settings(_env_file=None)
    assert {name: getattr(loaded, name) for name in Settings.model_fields} == {
        name: getattr(defaults, name) for name in Settings.model_fields
    }


def test_blank_values_mean_not_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "ARCHAEO_API_TOKEN=\nARCHAEO_QDRANT_URL=   \nARCHAEO_SQLITE_PATH=\nARCHAEO_EMBEDDING_DIMENSION=\n"
    )
    settings = Settings(_env_file=env)
    assert settings.api_token is None
    assert settings.qdrant_url is None
    assert settings.embedding_dimension is None
    assert settings.graph_path == Path("data") / "graph.sqlite3"


def test_lists_accept_commas_and_json(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'ARCHAEO_ALLOWED_GIT_HOSTS=github.com, git.example.com   # two hosts\nARCHAEO_CORS_ORIGINS=["https://a.example"]\n'
    )
    settings = Settings(_env_file=env)
    assert settings.allowed_git_hosts == ["github.com", "git.example.com"]
    assert settings.cors_origins == ["https://a.example"]


def test_production_needs_a_token() -> None:
    with pytest.raises(ValidationError, match="ARCHAEO_API_TOKEN"):
        Settings(environment="production", _env_file=None)
    with pytest.raises(ValidationError, match="ARCHAEO_API_TOKEN"):
        Settings(environment="production", api_token="  ", _env_file=None)
    assert Settings(environment="production", api_token="s3cret", _env_file=None).api_token is not None


def test_workers_need_shared_stores() -> None:
    with pytest.raises(ValidationError, match="Qdrant server"):
        Settings(job_backend="celery", _env_file=None)
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(vector_backend="memory", qdrant_url="http://qdrant:6333", _env_file=None)
