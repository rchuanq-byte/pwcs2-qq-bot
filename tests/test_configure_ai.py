from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "configure_ai.py"
SPEC = importlib.util.spec_from_file_location("configure_ai", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_models_response() -> None:
    assert MODULE.parse_models_response(
        {
            "data": [
                {"id": "deepseek-v4-flash"},
                {"id": "another/model"},
                {"id": "deepseek-v4-flash"},
            ]
        }
    ) == ("deepseek-v4-flash", "another/model")


def test_proxy_config_read_does_not_change_other_env(tmp_path, monkeypatch):
    monkeypatch.delenv("USTC_LLM_PROXY_URL", raising=False)
    path = tmp_path / ".env"
    path.write_text("OTHER=keep\nUSTC_LLM_PROXY_URL=http://local:7890\n")
    assert MODULE.read_configured_proxy(path) == "http://local:7890"
    monkeypatch.setenv("USTC_LLM_PROXY_URL", "http://override:7890")
    assert MODULE.read_configured_proxy(path) == "http://override:7890"


def test_configure_ai_uses_verified_https_tunnel(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def read(self, limit):
            return b'{"data":[{"id":"deepseek-flash"}]}'

    class Connection:
        def __init__(self, host, **kwargs):
            seen['host'] = host
            seen['context'] = kwargs['context']

        def set_tunnel(self, host, **kwargs):
            seen['tunnel'] = host
            seen['proxy_auth'] = kwargs['headers']['Proxy-Authorization']

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(MODULE.http.client, 'HTTPSConnection', Connection)
    assert MODULE.fetch_models('sk-fake', 'http://user:secret@127.0.0.1:7890') == ('deepseek-flash',)
    assert seen['host'] == '127.0.0.1'
    assert seen['tunnel'] == MODULE.API_HOST
    assert seen['context'].check_hostname
    assert seen['proxy_auth'].startswith('Basic ')


def test_parse_models_response_rejects_empty_or_invalid_data() -> None:
    with pytest.raises(MODULE.ConfigurationError, match="无效数据"):
        MODULE.parse_models_response({"data": "bad"})
    with pytest.raises(MODULE.ConfigurationError, match="没有可用模型"):
        MODULE.parse_models_response({"data": [{"id": "bad model with spaces"}]})


def test_update_env_text_preserves_other_secrets_and_removes_duplicates() -> None:
    result = MODULE.update_env_text(
        "PERFECTWORLD_TOKEN=keep\n"
        "USTC_LLM_API_KEY=old\n"
        "USTC_LLM_API_KEY=duplicate\n",
        {
            "USTC_LLM_API_KEY": "sk-new-secret",
            "USTC_LLM_MODEL": "deepseek-v4-flash",
            "USTC_LLM_VISION_MODEL": "qwen3.6-chat",
        },
    )

    assert result == (
        "PERFECTWORLD_TOKEN=keep\n"
        "USTC_LLM_API_KEY=sk-new-secret\n"
        "\n"
        "USTC_LLM_MODEL=deepseek-v4-flash\n"
        "USTC_LLM_VISION_MODEL=qwen3.6-chat\n"
    )
