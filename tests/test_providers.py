from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

import pytest

from agent.mock import MockModelClient
from agent.repair import DEFAULT_MODEL, HF_BASE_URL, ModelClient, ModelError, create_client


def options(tmp_path, **kwargs):
    values = dict(provider=None, model=None, base_url=None,
                  env_file=str(tmp_path / "missing.env"), mock_scenario="good_patch")
    return argparse.Namespace(**(values | kwargs))


def test_hf_defaults_to_real_meta_llama(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN_MAIN", "test-only-placeholder")
    receipt = {"model_calls": []}
    client = create_client(options(tmp_path), receipt)
    assert isinstance(client, ModelClient)
    assert client.model == DEFAULT_MODEL
    assert client.base_url == HF_BASE_URL
    assert receipt["model"] == {"provider": "hf", "mode": "live", "id": DEFAULT_MODEL}


def test_dotenv_process_and_cli_precedence_including_muse(tmp_path, monkeypatch):
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        "MODEL_ID=dotenv-model\nHF_TOKEN_MAIN=test-only-dotenv\nMODEL_PROVIDER=hf\n"
    )
    monkeypatch.setenv("MODEL_ID", "meta-models/Muse-Spark-Open-816B-A42B")
    args = options(tmp_path, env_file=str(env_file))
    client = create_client(args, {"model_calls": []})
    assert client.model == "meta-models/Muse-Spark-Open-816B-A42B"
    args.model = DEFAULT_MODEL
    assert create_client(args, {"model_calls": []}).model == DEFAULT_MODEL


def test_optional_llama_endpoint_is_configured_only_via_env_or_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "llama")
    monkeypatch.setenv("LLAMA_API_BASE", "https://example.com/v1")
    monkeypatch.setenv("LLAMA_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("MODEL_ID", "Llama-configured-model")
    receipt = {"model_calls": []}
    client = create_client(options(tmp_path), receipt)
    assert client.base_url == "https://example.com/v1"
    assert client.tokens == [("LLAMA_API_KEY", "test-only-placeholder")]
    assert client.provider == "llama"
    assert receipt["model"]["provider"] == "llama"
    assert "example.com" not in json.dumps(receipt)


@pytest.mark.parametrize("content", ['{"ok": true}', {"type": "text", "text": '{"ok": true}'}])
def test_native_meta_llama_request_and_response_format(content, monkeypatch):
    receipt = {"model_calls": []}
    client = ModelClient("https://example.com/v1", "Llama-configured-model",
                         [("LLAMA_API_KEY", "test-only-placeholder")], receipt, provider="llama")
    captured = {}

    def post(payload, token, timeout):
        captured.update(payload)
        return 200, {"completion_message": {"role": "assistant", "content": content}}

    monkeypatch.setattr(client, "_post", post)
    assert json.loads(client.chat([])) == {"ok": True}
    assert captured["max_completion_tokens"] == 8000
    assert "max_tokens" not in captured
    assert receipt["model_calls"][0]["key_env"] == "LLAMA_API_KEY"


@pytest.mark.parametrize("provider", ["hf", "llama"])
def test_live_provider_without_credentials_never_becomes_mock(tmp_path, provider):
    with pytest.raises(ModelError):
        create_client(options(tmp_path, provider=provider), {"model_calls": []})


def test_mock_is_explicit_and_ignores_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "llama")
    monkeypatch.setenv("LLAMA_API_KEY", "test-only-placeholder")
    receipt = {"model_calls": []}
    client = create_client(options(tmp_path, provider="mock"), receipt)
    assert isinstance(client, MockModelClient)
    assert receipt["model"]["mode"] == "mock"
    assert receipt["model"]["id"] == "fixture-mock-v1"


def test_live_provider_rejects_mock_scenario(tmp_path):
    with pytest.raises(ModelError, match="requires --provider mock"):
        create_client(options(tmp_path, provider="hf", mock_scenario="bad_patch"), {})


def test_live_transport_sends_chat_request_and_records_usage(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": '{"queries": ["parse_duration"]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            }).encode()

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["authorized"] = request.has_header("Authorization")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    receipt = {"model_calls": []}
    client = ModelClient(HF_BASE_URL, DEFAULT_MODEL,
                         [("HF_TOKEN_MAIN", "test-only-placeholder")], receipt)
    result = client.chat([{"role": "user", "content": "Return JSON"}], label="queries")
    assert json.loads(result)["queries"] == ["parse_duration"]
    assert captured["url"] == HF_BASE_URL + "/chat/completions"
    assert captured["payload"]["model"] == DEFAULT_MODEL
    assert captured["authorized"]
    assert receipt["model_calls"][0]["prompt_tokens"] == 12
    assert "test-only-placeholder" not in json.dumps(receipt)


def test_auth_failure_rotates_token_and_empty_response_retries(monkeypatch):
    receipt = {"model_calls": []}
    client = ModelClient(HF_BASE_URL, DEFAULT_MODEL,
                         [("HF_TOKEN_MAIN", "test-only-main"),
                          ("HF_TOKEN_FALLBACK", "test-only-fallback")], receipt)
    budgets = []
    responses = iter([
        (401, {"error": "do not log this body"}),
        (200, {"choices": [{"message": {"content": ""}}]}),
        (200, {"choices": [{"message": {"content": "{}"}}]}),
    ])

    def post(payload, token, timeout):
        budgets.append(payload["max_tokens"])
        return next(responses)

    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr("agent.repair.time.sleep", lambda _: None)
    assert client.chat([]) == "{}"
    assert budgets == [8000, 8000, 24000]
    assert [call["key_env"] for call in receipt["model_calls"]][:2] == [
        "HF_TOKEN_MAIN", "HF_TOKEN_FALLBACK",
    ]
    assert "do not log" not in json.dumps(receipt)


def test_provider_error_body_and_connection_details_are_not_reported(monkeypatch):
    receipt = {"model_calls": []}
    client = ModelClient(HF_BASE_URL, DEFAULT_MODEL,
                         [("HF_TOKEN_MAIN", "test-only-placeholder")], receipt)
    monkeypatch.setattr(client, "_post", lambda *a: (400, {"error": "private-detail"}))
    with pytest.raises(ModelError, match="HTTP 400") as exc:
        client.chat([])
    assert "private-detail" not in str(exc.value)
    assert "private-detail" not in json.dumps(receipt)

    def failing_urlopen(*args, **kwargs):
        raise urllib.error.URLError("private-detail")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    assert ModelClient._post(client, {}, "test-only-placeholder", 1) == (0, {})
