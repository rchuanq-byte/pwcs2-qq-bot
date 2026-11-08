import json

import httpx
import pytest

from pwcs2_bot.ai_chat import AIChatError, AIChatService
from pwcs2_bot.channel import PWCS2Config


async def test_native_vision_is_one_request_and_does_not_retain_images():
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "图片回答"}}]})

    service = AIChatService("sk-test", cooldown_seconds=0, transport=httpx.MockTransport(handle))
    images = ("data:image/png;base64,aGVsbG8=", "data:image/png;base64,d29ybGQ=")
    try:
        assert await service.chat(
            chat_id="group:1", requester_id="1", content="看图", speaker="群友",
            image_data_urls=images,
        ) == "图片回答"
        assert len(bodies) == 1
        body = bodies[0]
        assert body["model"] == "deepseek-flash"
        assert body["messages"][-1]["content"] == [
            {"type": "text", "text": "群友：看图"},
            *({"type": "image_url", "image_url": {"url": url}} for url in images),
        ]
        assert "qwen_vision_helper" not in json.dumps(body)
        assert "data:image" not in json.dumps(list(service._history_for("group:1")))
        await service.chat(chat_id="group:1", requester_id="2", content="继续")
        assert len(bodies) == 2
        assert "data:image" not in json.dumps(bodies[1])
        assert bodies[1]["messages"][-3]["content"] == "群友：看图\n[images: 2]"
    finally:
        await service.close()


async def test_native_image_failure_releases_capacity_without_history():
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if isinstance(body["messages"][-1]["content"], list):
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "恢复"}}]})

    service = AIChatService(
        "sk-test", cooldown_seconds=0, max_concurrent_requests=1,
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(AIChatError, match="HTTP 503"):
            await service.chat(
                chat_id="group:1", requester_id="1", content="看图",
                image_data_urls=("data:image/png;base64,aGVsbG8=",),
            )
        assert not service._history_for("group:1")
        assert await service.chat(
            chat_id="group:1", requester_id="1", content="文字请求",
        ) == "恢复"
        assert len(bodies) == 2
        assert "看图" not in json.dumps(bodies[1], ensure_ascii=False)
    finally:
        await service.close()


def test_channel_defaults_match_native_multimodal_model():
    config = PWCS2Config.model_validate({})
    assert config.ai_model == config.ai_vision_model == "deepseek-flash"
