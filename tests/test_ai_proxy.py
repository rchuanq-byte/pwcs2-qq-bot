import httpx
import pytest

from pwcs2_bot.ai_chat import AIChatService


@pytest.mark.parametrize('url', [
    'socks5://user:secret@proxy:1080',
    'http://user:secret@proxy:99999',
    'http://user:secret@proxy:0',
    'http://user:secret@proxy:7890/path',
    'http://user:secret@proxy:7890?key=secret',
    'http://user:secret@proxy:7890\nX',
])
def test_ai_proxy_validation_never_echoes_credentials(url):
    with pytest.raises(ValueError) as error:
        AIChatService('sk-test', proxy_url=url)
    assert 'secret' not in str(error.value)
    assert url not in str(error.value)


async def test_proxy_is_explicit_and_global_proxy_environment_is_ignored(monkeypatch):
    captured = {}
    original = httpx.AsyncClient

    def make_client(**kwargs):
        captured.update(kwargs)
        # Keep the test offline; real forwarding is verified on the server.
        kwargs['proxy'] = None
        return original(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    monkeypatch.setenv('HTTPS_PROXY', 'http://unrelated-proxy.invalid:9999')
    service = AIChatService('sk-test', proxy_url='http://user:secret@proxy:7890')
    try:
        assert captured['proxy'] == 'http://user:secret@proxy:7890'
        assert captured['trust_env'] is False
    finally:
        await service.close()


async def test_empty_or_unexpanded_proxy_keeps_direct_connections():
    for url in ('', '${USTC_LLM_PROXY_URL}'):
        service = AIChatService('sk-test', proxy_url=url)
        await service.close()
