"""One outbound authority across AstrBot, imported plugins and CS callbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pwcs2_bot.channel import _EXPLICIT_REPLY
from pwcs2_bot.chat_profiles import chat_only, conversation_groups, mention_only
from pwcs2_bot.dafeiyu_persona import CHAT_EXTENSION_COMMANDS
from pwcs2_bot.moderation_approval import OWNER, approved_native_action

SEND_ACTIONS = frozenset(
    {
        "send_msg",
        "send_group_msg",
        "send_private_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "send_group_ai_record",
        "upload_group_file",
        "upload_private_file",
        "_send_group_notice",
        "send_group_notice",
    }
)
ALLOWED_AI_TOOLS = frozenset(
    {
        "find_group_members",
        "query_group_statistics",
        "search_niuniu_scripture",
    }
)


class OutboundBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    allowed: bool
    command: Any = None
    explicit: bool = False
    synthetic: bool = False


def reserve_automatic_turn(application, group_id: str) -> bool:
    """Keep quiet/eligibility guards while optionally deferring pacing to upstream."""
    if mention_only(application.config, group_id) or application._automatic_quiet_active():
        return False
    return application.companion.reserve_automatic_reply(
        group_id,
        upstream_managed=application.config.ai_companion_use_upstream_limits,
    )


def classify(application, raw: dict[str, Any], *, quoted_bot=False) -> Route:
    gid, uid = str(raw.get("group_id") or ""), str(raw.get("user_id") or "")
    kind = raw.get("message_type")
    cutoff, stamp = application.config.message_replay_ignore_before, raw.get("time")
    if cutoff is not None and isinstance(stamp, (int, float)) and stamp <= cutoff.timestamp():
        return Route(False)
    if raw.get("post_type") == "notice":
        return Route(gid in application.config.allowed_groups)
    if raw.get("post_type") == "request" and raw.get("request_type") == "group":
        return Route(gid in application.config.allowed_groups)
    if kind not in {"group", "private"} or not uid or uid == str(application._self_id):
        return Route(False)
    if kind == "private":
        return Route(application.config.private_replies_enabled, explicit=True)
    if gid not in conversation_groups(application.config) or not application.is_allowed(uid):
        return Route(False)
    cutoff, stamp = application.config.message_replay_ignore_before, raw.get("time")
    if cutoff is not None and isinstance(stamp, (int, float)) and stamp <= cutoff.timestamp():
        return Route(False)
    text, _, mention, reply = application._parse_segments(
        application._normalize_segments(raw.get("message")),
    )
    if mention_only(application.config, gid) and not mention:
        return Route(False)
    command = application.commands.parse(text)
    if chat_only(application.config, gid):
        head = text.strip().lstrip("/#").split(maxsplit=1)[0].casefold() if text.strip() else ""
        extension_command = (
            application.config.ai_upstream_extensions_enabled and head in CHAT_EXTENSION_COMMANDS
        )
        if (
            text.strip().startswith(("/", "#"))
            and not extension_command
            and not text.strip().casefold().startswith(("#ai", "/ai"))
        ):
            return Route(False)
        command = None  # No CS, economy, moderation or other business commands.
    explicit = bool(
        command
        or mention
        or quoted_bot
        or reply in application._bot_outbound_ids
        or text.strip().casefold().startswith(("#ai", "/ai"))
        or application.companion.wake_prompt(gid, text) is not None
    )
    return Route(True, command, explicit)


class AstrBotOutboundPolicy:
    def __init__(self, application, current_event):
        self.application = application
        self.current_event = current_event
        self._installed = None
        self._original = None
        self._guarded = None

    def validate(self, action: str, params: dict[str, Any], event=None):
        gid = str(params.get("group_id") or "")
        if (
            action in SEND_ACTIONS
            and mention_only(self.application.config, gid)
            and not (
                event is not None
                and event.get_extra("niuniu_mentioned")
                and str(event.get_group_id()) == gid
            )
        ):
            raise OutboundBlocked("Mention-only group requires a real @bot input")
        if chat_only(self.application.config, gid):
            if action.startswith("set_") or action in {"delete_msg", "group_poke", "send_poke"}:
                raise OutboundBlocked("Chat-only group has no administrative authority")
            if action in SEND_ACTIONS and not (
                action in {"send_msg", "send_group_msg"}
                and params.get("message_type", "group") == "group"
                and event is not None
                and event.get_extra("niuniu_ai")
                and event.get_extra("niuniu_chat_only")
                and str(event.get_group_id()) == gid
            ):
                raise OutboundBlocked(
                    "Chat-only group accepts only its own AI conversation replies"
                )
        if approved_native_action(event, action, params):
            return
        interaction = bool(
            event
            and event.get_extra("niuniu_interaction_notice")
            and gid == str(event.get_group_id())
            and gid in self.application.config.allowed_groups
        )
        if interaction and action == "group_poke":
            raw = event.message_obj.raw_message
            if (
                gid in self.application.config.allowed_groups
                and gid == str(event.get_group_id())
                and str(params.get("user_id")) == str(raw.get("user_id"))
                and str(raw.get("target_id")) == str(self.application._self_id)
            ):
                return
        if (
            action == "group_poke"
            and event
            and OWNER
            and str(event.get_sender_id()) == OWNER
            and gid in self.application.config.allowed_groups
            and gid == str(event.get_group_id())
            and event.get_extra("niuniu_explicit")
            and "戳" in event.message_str
            and str(params.get("user_id")) in event.message_str
        ):
            return
        if action == "group_poke" and event and event.get_extra("niuniu_notice_only"):
            raise OutboundBlocked("Poke notice does not authorize this target")
        if (
            self.application.config.ai_upstream_extensions_enabled
            and action == "set_group_add_request"
        ):
            raise OutboundBlocked("Join requests always require an exact human approval grant")
        if event and event.get_extra("niuniu_notice_only") and action.startswith("set_"):
            raise OutboundBlocked("Notice handlers require explicit human approval for management")
        if event is not None and event.get_extra("niuniu_ai"):
            if action.startswith("set_") or action in {
                "delete_msg",
                "group_poke",
                "friend_poke",
                "send_poke",
                "_send_group_notice",
                "send_group_notice",
                "upload_group_file",
                "upload_private_file",
            }:
                raise OutboundBlocked(
                    "Personality AI has no administrative or file-write authority"
                )
        if action not in SEND_ACTIONS:
            return
        private = (
            "private" in action
            or params.get("message_type") == "private"
            or bool(params.get("user_id") and not gid)
        )
        if private:
            if not self.application.config.private_replies_enabled:
                raise OutboundBlocked("Private QQ output is disabled")
        elif gid not in conversation_groups(self.application.config):
            raise OutboundBlocked("QQ group is outside the output allowlist")
        explicit = _EXPLICIT_REPLY.get() or bool(event and event.get_extra("niuniu_explicit"))
        if self.application._automatic_quiet_active() and not explicit:
            raise OutboundBlocked("Automatic output is temporarily paused")
        if (
            event is not None
            and not explicit
            and not interaction
            and not event.get_extra("niuniu_auto_reserved")
        ):
            raise OutboundBlocked("Automatic AI output has no reserved turn")

    def install(self, bot):
        api = bot._api
        if self._installed is api:
            return
        original = api.call_action

        async def guarded(action, **params):
            event = self.current_event()
            self.validate(action, params, event)
            result = await original(action=action, **params)
            if action in SEND_ACTIONS and isinstance(result, dict):
                mid = result.get("message_id")
                if isinstance(mid, (int, str)) and str(mid).lstrip("-").isdigit():
                    self.application._bot_outbound_ids.append(int(mid))
            return result

        api.call_action = guarded
        self._installed = api
        self._original, self._guarded = original, guarded

    def uninstall(self):
        if self._installed is not None and self._installed.call_action is self._guarded:
            self._installed.call_action = self._original
        self._installed = None
