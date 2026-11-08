"""Opt-in QQ voice tools: no paid service, local model or audio collection."""

from __future__ import annotations

import asyncio
import time


class NativeVoiceError(RuntimeError):
    pass


class NativeVoice:
    def __init__(self, call_action, *, enabled=False, reply_enabled=False, character=""):
        self.call_action = call_action
        self.enabled = enabled
        self.reply_enabled = reply_enabled
        self.character = character
        self._voices = {}
        self._semaphore = asyncio.Semaphore(1)

    async def transcribe(self, message_id):
        if not self.enabled or not isinstance(message_id, (int, str)) or len(str(message_id)) > 64:
            raise NativeVoiceError("语音转文字未启用，或没有可识别的消息。")
        if self._semaphore.locked():
            raise NativeVoiceError("正在处理上一条语音，请稍后再试或先发文字。")
        try:
            async with asyncio.timeout(25):
                async with self._semaphore:
                    response = await self.call_action(
                        "fetch_ptt_text", {"message_id": message_id}, timeout=22
                    )
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise ValueError
            text = str((response.get("data") or {}).get("text") or "").strip()
            if not text or len(text) > 2000:
                raise ValueError
            return text
        except Exception as exc:
            # Never expose raw NapCat errors, URLs or audio references to QQ.
            raise NativeVoiceError("这条语音暂时没能转成文字，请重试或改用文字。") from exc

    async def speak(self, group_id, text):
        if not self.enabled or not self.reply_enabled:
            raise NativeVoiceError("当前只启用语音转写，使用文字回答。")
        if not text or len(text) > 200:
            raise NativeVoiceError("这条回答较长，改用文字发送完整内容。")
        if self._semaphore.locked():
            raise NativeVoiceError("语音通道正在使用，先用文字回答。")
        try:
            async with asyncio.timeout(25):
                async with self._semaphore:
                    voice = self.character
                    if not voice:
                        cached = self._voices.get(group_id)
                        if cached is None or time.monotonic() - cached[0] > 600:
                            response = await self.call_action(
                                "get_ai_characters",
                                {"group_id": group_id, "chat_type": 1},
                                timeout=8,
                            )
                            if response.get("status") != "ok" or response.get("retcode") != 0:
                                raise ValueError
                            data = response.get("data") or []
                            voices = [
                                c.get("character_id")
                                for g in data
                                if isinstance(g, dict)
                                for c in g.get("characters", [])
                                if isinstance(c, dict) and c.get("character_id")
                            ]
                            self._voices[group_id] = (
                                time.monotonic(),
                                str(voices[0]) if voices else "",
                            )
                            if len(self._voices) > 32:
                                self._voices.pop(next(iter(self._voices)))
                        voice = self._voices[group_id][1]
                    if not voice:
                        raise NativeVoiceError("当前 QQ 账号没有可用的 AI 音色，先用文字回答。")
                    # This action sends a group voice message. It is called
                    # only after an explicit /ai语音 request, never by a probe.
                    response = await self.call_action(
                        "send_group_ai_record",
                        {"group_id": group_id, "character": voice, "text": text},
                        timeout=20,
                    )
                    if response.get("status") != "ok" or response.get("retcode") != 0:
                        raise ValueError
        except NativeVoiceError:
            raise
        except Exception as exc:
            raise NativeVoiceError("语音回复暂时不可用，先用文字回答。") from exc
