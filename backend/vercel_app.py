"""ASGI entrypoint for serverless hosts (Vercel Functions).

A function instance has no git binary, no worker process and no durable disk, so it
cannot ingest repositories. This entrypoint therefore serves a **read-only**
instance: the bundled demo graph is loaded from its snapshot when the instance
starts, the semantic index is rebuilt in memory, and every endpoint that would
write answers ``403``. Anything it needs to write goes under ``/tmp``.

The defaults below can be overridden with the usual ``ARCHAEO_*`` environment
variables, e.g. ``ARCHAEO_LLM_PROVIDER`` / ``ARCHAEO_LLM_MODEL`` /
``ARCHAEO_LLM_API_KEY`` to have a language model write the explanations.

To analyse your own repositories, run the full stack (``docker compose up``).
"""

from __future__ import annotations

import os

_SERVERLESS_DEFAULTS = {
    "ARCHAEO_DATA_DIR": "/tmp/archaeo",  # noqa: S108 - the only writable location on the host
    "ARCHAEO_READ_ONLY": "true",
    "ARCHAEO_DEMO_SOURCE": "snapshot",
    "ARCHAEO_VECTOR_BACKEND": "memory",
    "ARCHAEO_JOB_BACKEND": "inline",
}
for _key, _value in _SERVERLESS_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

from backend.main import create_app  # noqa: E402 - settings are read from the environment prepared above

app = create_app()
