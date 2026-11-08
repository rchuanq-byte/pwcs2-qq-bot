import subprocess
import sys

import pytest


def test_health_probe_does_not_import_bot_or_third_party_integrations():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import pwcs2_bot.healthcheck; "
         "assert 'pwcs2_bot.channel' not in sys.modules; "
         "assert 'nanobot' not in sys.modules; "
         "assert 'httpx' not in sys.modules; print('OK lightweight health import')"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout.strip() == "OK lightweight health import"


def test_public_channel_export_is_still_backwards_compatible():
    import pwcs2_bot
    from pwcs2_bot.channel import PWCS2Channel

    assert pwcs2_bot.PWCS2Channel is PWCS2Channel
    assert pwcs2_bot.PWCS2Channel is PWCS2Channel
    with pytest.raises(AttributeError):
        _ = pwcs2_bot.no_such_attribute
