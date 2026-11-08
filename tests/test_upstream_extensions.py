import ast
import base64
import io
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pwcs2_bot.astrbot_policy import AstrBotOutboundPolicy, OutboundBlocked
from pwcs2_bot.enhanced_media import EnhancedMedia, image_grid
from pwcs2_bot.moderation_approval import (
    OWNER,
    ApprovalError,
    ModerationApprovals,
    approved_action,
    approved_native_action,
)
from pwcs2_bot.public_fetch import (
    PublicMediaError,
    PublicResolver,
    fetch_public_bytes,
    validate_public_url,
)
from pwcs2_bot.upstream_extensions import (
    CHAT_TOOLS,
    GROUP_TOOLS,
    allowed_tools,
    configure_extensions,
)

ROOT = Path(__file__).resolve().parents[1]


class Event:
    def __init__(self, gid="1", uid=OWNER, **extra):
        self.gid, self.uid, self.extra = gid, uid, extra
        self.message_obj = SimpleNamespace(self_id="99", raw_message={})
        self.message_str = ""

    def get_group_id(self):
        return self.gid

    def get_sender_id(self):
        return self.uid

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost",
        "http://a.localhost/",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://172.18.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "https://user:pass@example.com/",
        "https://example.com:8080/",
    ],
)
def test_public_fetch_rejects_private_and_credentialled_urls(url):
    with pytest.raises(PublicMediaError):
        validate_public_url(url)


async def test_actual_dns_results_are_checked_not_only_url(monkeypatch):
    validate_public_url("https://example.com/page")
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: infos)
    assert (await PublicResolver().resolve("example.com", 443))[0]["host"] == "93.184.216.34"
    infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)))
    with pytest.raises(PublicMediaError, match="non-public"):
        await PublicResolver().resolve("example.com", 443)


async def test_inline_media_is_mime_and_size_bounded_and_never_reads_files():
    value = "data:image/png;base64," + base64.b64encode(b"abcd").decode()
    assert await fetch_public_bytes(value, maximum=4, data_mimes=("image/png",)) == b"abcd"
    for source, size in [(value, 3), (value.replace("png", "svg+xml"), 40), ("/etc/passwd", 40)]:
        with pytest.raises(PublicMediaError):
            await fetch_public_bytes(source, maximum=size, data_mimes=("image/png",))


def test_gif_uses_four_frames_and_same_group_history_expires():
    raw = io.BytesIO()
    pictures = [Image.new("RGB", (20, 20), c) for c in ("red", "green", "blue", "yellow", "black")]
    pictures[0].save(raw, format="GIF", save_all=True, append_images=pictures[1:])
    grid = Image.open(io.BytesIO(base64.b64decode(image_grid(raw.getvalue()).split(",")[1])))
    assert grid.size == (40, 40)
    assert grid.getpixel((0, 0)) == (255, 0, 0)
    assert grid.getpixel((20, 20)) == (255, 255, 0)
    now = [1000]
    media = EnhancedMedia(None, None, clock=lambda: now[0])
    segments = [{"type": "video", "data": {"url": "https://example.com/a.mp4"}}]
    media.remember("1", segments)
    assert media.previous("1", "刚才那段视频是什么", "video") == segments
    assert not media.previous("2", "刚才那段视频是什么", "video")
    assert not media.previous("1", "你好", "video")
    now[0] += 301
    assert not media.previous("1", "刚才那段视频是什么", "video")


async def test_video_cannot_interpret_playlist_or_read_local_file(monkeypatch):
    calls = []

    async def call(action, params):
        calls.append(action)
        return {"data": {"file": "/etc/passwd"}}

    media = EnhancedMedia(SimpleNamespace(_call_action=call), lambda: None)
    with pytest.raises(PublicMediaError):
        await media.describe_video([{"data": {"file": "/etc/passwd"}}])
    assert calls == ["get_file"]
    bad = "data:video/mp4;base64," + base64.b64encode(b"#EXTM3U\nfile:///etc/passwd").decode()
    with pytest.raises(PublicMediaError, match="MP4/WebM"):
        await media.describe_video([{"data": {"url": bad}}])


def test_extensions_are_free_scoped_and_keep_chat_only_without_business():
    env = {"NAPCAT_ACCOUNT": "99", "DSH_WEB_SEARCH_KEY": "old", "DSH_VOICE_API_KEY": "old"}
    configure_extensions(env, {"allowedGroups": ["1"], "aiUpstreamExtensionsEnabled": True})
    assert env["DSH_WEB_SEARCH_KEY"] == env["DSH_VOICE_API_KEY"] == ""
    assert env["DSH_NIUNIU_EDGE_ONLY"] == "1"
    assert env["DSH_VID_GEN"] == "0" and env["DSH_VID_CONCEPT_AUTO"] == "0"
    for module in ("POKE", "WELCOME", "GUARD", "JOINGUARD", "STICKER", "STEAL"):
        assert env[f"DSH_{module}_GROUPS"] == "1"
    cfg = SimpleNamespace(ai_chat_only_groups={"2"}, ai_upstream_extensions_enabled=True)
    assert allowed_tools(cfg, "2", {"stats"}) == CHAT_TOOLS
    assert allowed_tools(cfg, "1", {"stats"}) == CHAT_TOOLS | GROUP_TOOLS | {"stats"}
    cfg.ai_upstream_extensions_enabled = False
    assert allowed_tools(cfg, "2", {"stats"}) == set()
    assert allowed_tools(cfg, "1", {"stats"}) == {"stats"}


def test_approval_identity_expiry_single_use_limits_and_private_join_flags(tmp_path):
    now = [1000]
    queue = ModerationApprovals(tmp_path / "approval.db", {"1"}, clock=lambda: now[0])
    proposal = queue.propose("1", "11", "22", "mute", duration=60, reason="reason")
    assert queue.propose("1", "11", "22", "mute", duration=60)["id"] == proposal["id"]
    for group, actor in [("2", OWNER), ("1", "11")]:
        with pytest.raises(ApprovalError):
            queue.claim(proposal["id"], group, actor)
    queue.claim(proposal["id"], "1", OWNER, cancel=True)
    with pytest.raises(ApprovalError):
        queue.claim(proposal["id"], "1", OWNER)
    for duration in (0, 601):
        with pytest.raises(ApprovalError):
            queue.propose("1", "11", "22", "mute", duration=duration)
    with pytest.raises(ApprovalError):
        queue.propose("1", "11", OWNER, "kick")
    join = queue.propose("1", "11", "22", "approve_join", flag="PRIVATE_FLAG")
    assert "PRIVATE_FLAG" not in str(join) and "PRIVATE_FLAG" not in str(queue.pending("1", OWNER))
    now[0] += 301
    with pytest.raises(ApprovalError, match="过期"):
        queue.claim(join["id"], "1", OWNER)
    for i in range(4):
        queue.propose("1", "11", str(30 + i), "kick")
    with pytest.raises(ApprovalError, match="上限"):
        queue.propose("1", "11", "50", "kick")


def test_management_tool_uses_astrbot_supported_schema_type():
    source = (ROOT / "astrbot_plugins/astrbot_plugin_niuniu/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
    tool = next(
        n
        for n in main.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "propose_group_action"
    )
    assert "duration_seconds(number)" in ast.get_docstring(tool)


async def test_execution_rechecks_roles_and_never_retries_uncertain_result(tmp_path):
    queue = ModerationApprovals(tmp_path / "approval.db", {"1"})
    event, calls, role = Event(), [], ["member"]

    async def call(action, params):
        calls.append(action)
        if action == "get_group_member_info":
            return {"data": {"role": role[0] if str(params["user_id"]) == "99" else "member"}}
        assert approved_native_action(event, action, params)
        raise TimeoutError

    proposal = queue.propose("1", "11", "22", "mute", duration=60)
    assert "不是群管理员" in await queue.execute(proposal["id"], "1", OWNER, event, call, "99")
    assert calls == ["get_group_member_info"]
    role[0] = "admin"
    proposal = queue.propose("1", "11", "22", "mute", duration=60)
    assert "回执超时" in await queue.execute(proposal["id"], "1", OWNER, event, call, "99")
    assert calls.count("set_group_ban") == 1
    assert not approved_native_action(
        event, "set_group_ban", {"group_id": 1, "user_id": 22, "duration": 60}
    )
    with pytest.raises(ApprovalError):
        await queue.execute(proposal["id"], "1", OWNER, event, call, "99")
    assert calls.count("set_group_ban") == 1


def test_native_approval_is_exact_and_chat_only_and_notices_cannot_bypass():
    config = SimpleNamespace(
        ai_upstream_extensions_enabled=True,
        ai_chat_only_groups={"2"},
        allowed_groups={"1"},
        private_replies_enabled=False,
    )
    app = SimpleNamespace(config=config, _self_id="99", _automatic_quiet_active=lambda: False)
    policy = AstrBotOutboundPolicy(app, lambda: None)
    event = Event(niuniu_ai=True, niuniu_admin_confirmation=True)
    params = {"group_id": 1, "user_id": 22, "duration": 60}
    with pytest.raises(OutboundBlocked):
        policy.validate("set_group_ban", params, event)
    with approved_action("1", "set_group_ban", params):
        policy.validate("set_group_ban", params, event)
        for wrong in (
            {**params, "duration": 600},
            {**params, "user_id": 23},
            {**params, "self_id": "98"},
        ):
            with pytest.raises(OutboundBlocked):
                policy.validate("set_group_ban", wrong, event)
        with pytest.raises(OutboundBlocked):
            policy.validate("set_group_ban", {**params, "group_id": 2}, Event("2", niuniu_ai=True))
    with pytest.raises(OutboundBlocked):
        policy.validate("set_group_add_request", {"flag": "fake", "approve": True})
    notice = Event(uid="22", niuniu_notice_only=True, niuniu_interaction_notice=True)
    notice.message_obj.raw_message = {"user_id": "22", "target_id": "99"}
    policy.validate("group_poke", {"group_id": 1, "user_id": 22}, notice)
    with pytest.raises(OutboundBlocked):
        policy.validate("group_poke", {"group_id": 1, "user_id": 23}, notice)
    with pytest.raises(OutboundBlocked):
        policy.validate("set_group_ban", params, notice)


async def test_managed_vendor_moderation_never_falls_through_to_native(monkeypatch):
    monkeypatch.setenv("NIUNIU_MANAGED_EXTENSIONS", "1")
    calls = []

    async def propose(*args, **kwargs):
        calls.append((args, kwargs))

    for module, method, args in [
        ("guard", "_do_ban", (None, "1", "22", "name", 86400, "flood", "reason")),
        ("guard", "_do_kick", (None, "1", "22", "name", "reason")),
        ("guard", "_say", (Event(uid="22"), "warning")),
        ("joinguard", "_decide", (None, "1", "22", "PRIVATE", "answer", "approve", "reason")),
    ]:
        tree = ast.parse(
            (ROOT / f"vendor/dafeiyu/dsh-{module}/main.py").read_text(encoding="utf-8")
        )
        klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
        node = next(
            n for n in klass.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method
        )
        node.decorator_list = []
        fixture = ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                node,
            ],
            type_ignores=[],
        )
        namespace = {"os": os}
        exec(compile(ast.fix_missing_locations(fixture), "vendor-moderation", "exec"), namespace)
        instance = SimpleNamespace(context=SimpleNamespace(niuniu_moderation_propose=propose))
        # All native APIs/other helpers are deliberately absent: fallthrough would fail.
        await namespace[method](instance, *args)
        instance.context = SimpleNamespace()
        await namespace[method](instance, *args)
    assert len(calls) == 4 and calls[0][0][3] == 600
