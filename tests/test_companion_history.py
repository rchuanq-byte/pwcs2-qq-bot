import httpx

from pwcs2_bot.ai_chat import AIChatService


async def test_unpublished_automatic_suggestion_never_enters_shared_history():
    requests = []

    def handler(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "建议回答"}}]})

    service = AIChatService("sk-test", transport=httpx.MockTransport(handler), cooldown_seconds=60)
    try:
        await service.chat(
            chat_id="group:1",
            requester_id="1",
            content="上一个话题",
            retain_history=False,
            use_request_cooldown=False,
        )
        await service.chat(chat_id="group:1", requester_id="1", content="新话题")
        assert len(requests[-1]["messages"]) == 2
        assert requests[-1]["messages"][-1]["content"] == "新话题"
    finally:
        await service.close()
