from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_provider_env(monkeypatch):
    """Never let a developer's provider settings/credentials affect offline tests."""
    for name in (
        "MODEL_PROVIDER", "MODEL_ID", "HF_BASE_URL", "HF_TOKEN_MAIN",
        "HF_TOKEN_FALLBACK", "LLAMA_API_BASE", "LLAMA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
