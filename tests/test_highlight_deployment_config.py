from __future__ import annotations

import re
from pathlib import Path

from deploy.install_build_dependencies import _requirements

_ROOT = Path(__file__).parents[1]


def _service_block(compose: str, name: str) -> str:
    lines = compose.splitlines()
    header = f"  {name}:"
    start = lines.index(header)
    captured: list[str] = []
    for line in lines[start + 1 :]:
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line) or (
            line and not line.startswith(" ")
        ):
            break
        captured.append(line)
    return "\n".join(captured)


def test_highlight_services_use_secret_allowlists_and_safe_resource_defaults() -> None:
    compose = (_ROOT / "compose.yaml").read_text(encoding="utf-8")
    bot = _service_block(compose, "bot")
    controller = _service_block(compose, "highlight-controller")
    api = _service_block(compose, "highlight-api")

    assert "env_file:" in bot
    assert "env_file:" not in controller
    assert "env_file:" not in api
    assert 'PERFECTWORLD_PWA_ACCESS_TOKEN: ""' in bot
    assert 'PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON: "{}"' in bot
    assert 'PWCS2_PWA_DEMO_ENABLED: "0"' in bot
    for forbidden in (
        "USTC_LLM_API_KEY",
        "PERFECTWORLD_TOKEN",
        "STEAM_WEB_API_KEY",
        "NAPCAT_ONEBOT_TOKEN",
        "PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON",
    ):
        assert forbidden not in controller
    assert "PERFECTWORLD_PWA_ACCESS_TOKEN:" in controller
    assert "PWCS2_PWA_PUBLIC_IP:" in controller
    assert "PERFECTWORLD_MY_STEAM_ID:" in controller
    assert "PWCS2_HIGHLIGHT_DATA_DIR:" in controller
    assert "PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH:" in controller
    assert "PWCS2_HIGHLIGHT_SHARED_OUTPUT_GID:" in controller
    assert "PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON:" in api
    assert "PERFECTWORLD_PWA_ACCESS_TOKEN" not in api
    assert "mem_limit: ${HIGHLIGHT_CONTROLLER_MEMORY_LIMIT:-768m}" in controller
    assert "memswap_limit: ${HIGHLIGHT_CONTROLLER_MEMORY_SWAP_LIMIT:-1280m}" in controller
    assert "healthcheck:\n      disable: true" in controller
    assert "target: bot-runtime" in bot
    assert "target: highlight-controller-runtime" in controller
    assert 'PWA_WHEELHOUSE_URL: "${PWA_WHEELHOUSE_URL:-}"' in controller
    assert (
        'PWA_WHEELHOUSE_TRUSTED_HOST: "${PWA_WHEELHOUSE_TRUSTED_HOST:-}"'
        in controller
    )
    assert "PWCS2_HIGHLIGHT_IMAGE" in controller
    assert "./data/bot:/data" not in controller
    assert "./data/bot:/data" not in api
    assert "./data/bot/highlights:" in controller
    assert "./data/bot/demos:/data/demos" in controller
    assert "./data/bot/demo-import:/data/demo-import:ro" in controller
    assert "./data/bot/highlights:" in api


def test_highlight_data_and_video_limits_share_one_deployment_configuration() -> None:
    compose = (_ROOT / "compose.yaml").read_text(encoding="utf-8")
    config = (_ROOT / "config" / "config.json").read_text(encoding="utf-8")
    controller = _service_block(compose, "highlight-controller")
    api = _service_block(compose, "highlight-api")

    assert "PWCS2_HIGHLIGHT_DATA_DIR:" in controller
    assert "PWCS2_HIGHLIGHT_DATA_DIR:" in api
    assert '"highlightDataPath": "${PWCS2_HIGHLIGHT_DATA_DIR}"' in config
    assert (
        '"highlightIntentDatabasePath": '
        '"${PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH}"' in config
    )
    assert "PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES:" in api
    assert (
        '"highlightInlineVideoMaxBytes": "${PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES}"'
        in config
    )


def test_controller_preserves_napcat_read_access_to_render_directory() -> None:
    controller = (_ROOT / "src" / "pwcs2_bot" / "highlight_controller.py").read_text(
        encoding="utf-8"
    )

    assert "shared_root_gid=args.shared_data_gid" in controller
    assert "shared_output_gid=args.shared_data_gid" in controller


def test_data_initializer_restores_only_highlight_mount_root_group_access() -> None:
    override = (_ROOT / "compose.override.yaml").read_text(encoding="utf-8")

    assert 'HIGHLIGHT_SHARED_GID: "${PWCS2_HIGHLIGHT_SHARED_OUTPUT_GID:-1000}"' in override
    assert "chown -R 10001:10001 /data" in override
    assert "for path in /data/highlights /data/demos /data/demo-import" in override
    assert 'chown 10001:"$${HIGHLIGHT_SHARED_GID}" "$${path}"' in override
    assert 'chmod 0770 "$${path}"' in override
    assert 'chown 10001:"$${HIGHLIGHT_SHARED_GID}" /data/highlights/renders' in override
    assert "chmod 0750 /data/highlights/renders" in override


def test_pwa_signer_is_installed_only_in_the_controller_image_target() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    installer = (_ROOT / "deploy" / "install_build_dependencies.py").read_text(
        encoding="utf-8"
    )

    base, controller_and_bot = dockerfile.split(
        "FROM runtime-base AS highlight-controller-runtime", 1
    )
    controller, bot = controller_and_bot.split("FROM runtime-base AS bot-runtime", 1)
    assert "--only-optional-group pwa-demo" not in base
    assert "--only-optional-group pwa-demo" in controller
    assert "PIP_NO_INDEX=1" in controller
    assert 'PIP_FIND_LINKS="$PWA_WHEELHOUSE_URL"' in controller
    assert 'PIP_TRUSTED_HOST="$PWA_WHEELHOUSE_TRUSTED_HOST"' in controller
    assert "pwa-demo" not in bot
    assert 'get("optional-dependencies"' in installer
    assert "only_optional_group=args.only_optional_group" in installer
    assert not any(item.startswith("cs-demo-downloader") for item in _requirements())
    assert _requirements(only_optional_group="pwa-demo") == [
        "cs-demo-downloader==1.1.0",
        "demoparser2==0.41.4",
    ]


def test_highlight_check_uses_bounded_exec_and_unambiguous_container_lookup() -> None:
    script = (_ROOT / "deploy" / "check-highlights.sh").read_text(encoding="utf-8")

    assert "compose ps --format json" not in script
    assert 'compose ps --status running -q "$service"' in script
    assert '"${#running_ids[@]}" -eq 1' in script
    assert "Bot 镜像未安装 PWA 签名或 Demo 解析组件" in script
    assert "timeout --signal=TERM --kill-after=5s 15s" in script
    assert "timeout --signal=TERM --kill-after=5s 30s" in script
    assert "timeout --signal=TERM --kill-after=5s 20s" in script
    assert 'public_ip=str(public_address)' in script
    assert "timestamp=1_700_000_000" in script


def test_update_runs_bounded_highlight_preflight_only_for_enabled_profile() -> None:
    script = (_ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "HIGHLIGHTS_PROFILE_ENABLED=0" in script
    assert "HIGHLIGHTS_PROFILE_ENABLED=1" in script
    assert 'highlight_enabled="${HIGHLIGHT_ENABLED:-$file_highlight_enabled}"' in script
    assert 'case "${highlight_enabled,,}" in' in script
    assert "timeout --signal=TERM --kill-after=10s 120s" in script
    assert 'bash "$ROOT_DIR/deploy/check-highlights.sh"' in script
