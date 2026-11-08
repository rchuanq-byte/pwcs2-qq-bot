import asyncio
import json

import httpx
import pytest

from pwcs2_bot.ai_chat import AIChatError, AIChatService
from pwcs2_bot.ai_tool_protocol import read_tool_turn, validate_turn


class Toolbox:
    definitions = [
        {
            "type": "function",
            "function": {
                "name": "query_group_statistics",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    context = "Only use this request's read-only data."

    def __init__(self):
        self.calls = []

    async def execute(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "wins": 7, "losses": 3}


def call(args='{"kind":"cs_recent_matches"}', call_id="test-call"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "query_group_statistics",
            "arguments": args,
        },
    }


def response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


async def test_tool_result_round_trip_then_final_answer_only_in_history():
    bodies = []
    toolbox = Toolbox()

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return response(
                {"content": None, "reasoning_content": "private reasoning", "tool_calls": [call()]}
            )
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["tool_call_id"] == "test-call"
        assert json.loads(body["messages"][-1]["content"])["wins"] == 7
        assert body["messages"][-2]["reasoning_content"] == "private reasoning"
        return response({"content": "近十场7胜3负。"})

    ai = AIChatService("sk-test", cooldown_seconds=0, transport=httpx.MockTransport(handle))
    try:
        answer = await ai.chat(
            chat_id="group:1", requester_id="1", content="我最近怎么样", toolbox=toolbox
        )
        assert answer == "近十场7胜3负。"
        assert len(toolbox.calls) == 1
        history = list(ai._history_for("group:1"))
        assert len(history) == 2
        assert "private reasoning" not in json.dumps(history)
        assert "tool_call" not in json.dumps(history)
        assert bodies[0]["tools"] == toolbox.definitions
    finally:
        await ai.close()


async def test_tool_stream_reassembles_fragmented_arguments_and_ignores_usage_chunk():
    lines = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "abc",
                                "type": "function",
                                "function": {
                                    "name": "query_group_statistics",
                                    "arguments": '{"kind":',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"coins"}'}}]}}
            ]
        },
        {"choices": [], "usage": {"total_tokens": 10}},
    ]
    stream = "".join("data: " + json.dumps(part) + "\n\n" for part in lines) + "data: [DONE]\n\n"
    r = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)
    turn = await read_tool_turn(r)
    assert turn["tool_calls"][0]["function"]["arguments"] == '{"kind":"coins"}'
    assert turn["tool_calls"][0]["id"] == "abc"


@pytest.mark.parametrize("args", ["[]", "{bad", "null"])
async def test_bad_arguments_are_not_executed_and_model_can_recover(args):
    toolbox = Toolbox()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return response({"tool_calls": [call(args)]})
        body = json.loads(request.content)
        assert json.loads(body["messages"][-1]["content"])["error"] == "invalid_arguments"
        return response({"content": "请确认查询对象。"})

    ai = AIChatService("sk-test", transport=httpx.MockTransport(handle))
    try:
        assert await ai.chat(chat_id="g", requester_id="1", content="查", toolbox=toolbox)
        assert not toolbox.calls
    finally:
        await ai.close()


async def test_tool_budget_stops_loop_and_forces_final_answer():
    toolbox = Toolbox()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if body.get("tool_choice") == "none":
            return response({"content": "根据已查数据回答。"})
        return response({"tool_calls": [call(call_id=f"{count}-{i}") for i in range(3)]})

    ai = AIChatService("sk-test", transport=httpx.MockTransport(handle))
    try:
        assert await ai.chat(chat_id="g", requester_id="1", content="比较", toolbox=toolbox)
        assert len(toolbox.calls) == 6
        assert count == 3
    finally:
        await ai.close()


async def test_tools_and_native_images_use_same_model_without_losing_current_images():
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return response({"content": "这是红色。"})

    ai = AIChatService("sk-test", transport=httpx.MockTransport(handle))
    try:
        await ai.chat(
            chat_id="g",
            requester_id="1",
            content="图片颜色",
            image_data_urls=("data:image/png;base64,TEST",),
            toolbox=Toolbox(),
        )
        assert len(bodies) == 1
        assert bodies[0]["model"] == "deepseek-flash"
        assert bodies[0]["messages"][-1]["content"][1]["type"] == "image_url"
        assert "data:image" not in json.dumps(list(ai._history_for("g")))
    finally:
        await ai.close()


async def test_cancelled_tool_releases_ai_slot_and_writes_no_history():
    entered = asyncio.Event()

    class WaitingToolbox(Toolbox):
        async def execute(self, name, args):
            entered.set()
            await asyncio.Event().wait()

    ai = AIChatService(
        "sk-test",
        cooldown_seconds=0,
        max_concurrent_requests=1,
        transport=httpx.MockTransport(lambda r: response({"tool_calls": [call()]})),
    )
    try:
        task = asyncio.create_task(
            ai.chat(chat_id="g", requester_id="1", content="查", toolbox=WaitingToolbox())
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ai._semaphore._value == 1
        assert not ai._history_for("g")
    finally:
        await ai.close()


def test_malformed_or_duplicate_tool_calls_are_rejected_before_execution():
    with pytest.raises(ValueError):
        validate_turn({"tool_calls": [call(), call()]})
    with pytest.raises(ValueError):
        validate_turn({"tool_calls": [call("x" * 8193)]})
    with pytest.raises(ValueError):
        validate_turn({"tool_calls": [call(call_id=str(i)) for i in range(7)]})


async def test_http_failure_releases_slot_without_inventing_answer():
    ai = AIChatService("sk-test", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    try:
        with pytest.raises(AIChatError, match="HTTP 503"):
            await ai.chat(chat_id="g", requester_id="1", content="查", toolbox=Toolbox())
        assert not ai._history_for("g")
    finally:
        await ai.close()


async def test_large_tool_result_replaced_with_valid_json_not_truncated_data():
    class LargeToolbox(Toolbox):
        async def execute(self, name, args):
            return {"ok": True, "entries": ["x" * 13_000]}

    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return response({"tool_calls": [call()]})
        body = json.loads(request.content)
        assert json.loads(body["messages"][-1]["content"])["error"] == "result_too_large"
        return response({"content": "请缩小查询范围。"})

    ai = AIChatService("sk-test", transport=httpx.MockTransport(handle))
    try:
        assert await ai.chat(chat_id="g", requester_id="1", content="查询", toolbox=LargeToolbox())
    finally:
        await ai.close()


async def test_tool_failure_passed_back_as_unavailable_not_fabricated_zero():
    class FailedToolbox(Toolbox):
        async def execute(self, name, args):
            raise TimeoutError("sensitive internal address")

    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return response({"tool_calls": [call()]})
        content = json.loads(request.content)["messages"][-1]["content"]
        assert json.loads(content)["error"] == "data_unavailable"
        assert "sensitive" not in content and '"wins"' not in content
        return response({"content": "查询暂不可用。"})

    ai = AIChatService("sk-test", transport=httpx.MockTransport(handle))
    try:
        assert await ai.chat(chat_id="g", requester_id="1", content="查询", toolbox=FailedToolbox())
    finally:
        await ai.close()
