"""Bounded OpenAI-compatible tool-call parsing; never executes model code."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

MAX_TOOL_CALLS = 6


class ChatToolbox(Protocol):
    definitions: list[dict[str, Any]]
    context: str

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def validate_turn(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ValueError("invalid tool response")
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    if not isinstance(content, str) or len(content) > 100_000:
        raise ValueError("invalid content")
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
        raise ValueError("too many tool calls")
    result: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        seen = set()
        clean = []
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("invalid tool call")
            call_id = call.get("id")
            function = call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 200
                or call_id in seen
                or not isinstance(function, dict)
            ):
                raise ValueError("invalid tool identity")
            name, arguments = function.get("name"), function.get("arguments")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 100
                or not isinstance(arguments, str)
                or len(arguments) > 8192
            ):
                raise ValueError("invalid tool arguments")
            seen.add(call_id)
            clean.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        result["tool_calls"] = clean
        # Some reasoning providers require this field to continue a tool turn.
        # It is echoed only within this request, never logged or put in history.
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            if len(reasoning) > 100_000:
                raise ValueError("reasoning response too large")
            result["reasoning_content"] = reasoning
    return result


async def read_tool_turn(response: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" not in response.headers.get("content-type", ""):
        await response.aread()
        return validate_turn(response.json()["choices"][0]["message"])
    message: dict[str, Any] = {"content": "", "reasoning_content": ""}
    calls: dict[int, dict[str, Any]] = {}
    size = 0
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        size += len(raw)
        if size > 2_000_000:
            raise ValueError("tool stream too large")
        payload = json.loads(raw)
        choices = payload.get("choices") or []
        if not choices:  # Usage-only final chunk.
            continue
        delta = choices[0].get("delta") or {}
        for key in ("content", "reasoning_content"):
            part = delta.get(key)
            if isinstance(part, str):
                message[key] += part
        for fragment in delta.get("tool_calls") or []:
            if fragment.get("type", "function") != "function":
                raise ValueError("unsupported tool type")
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                raise ValueError("invalid tool index")
            call = calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if fragment.get("id"):
                call["id"] += fragment["id"]
            function = fragment.get("function") or {}
            for key in ("name", "arguments"):
                if function.get(key):
                    call["function"][key] += function[key]
    message["tool_calls"] = [calls[key] for key in sorted(calls)]
    return validate_turn(message)
