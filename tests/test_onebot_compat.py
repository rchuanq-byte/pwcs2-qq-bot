import asyncio

from pwcs2_bot.onebot_compat import install_result_guard


async def test_late_or_duplicate_echo_cannot_break_socket_or_overwrite_result():
    class Store:
        _futures = {}

        @classmethod
        def add(cls, result):
            if isinstance(result.get("echo"), dict):
                future = cls._futures.get(result["echo"].get("seq"))
                if future is not None:
                    future.set_result(result)

    original = Store.__dict__["add"]
    cleanup = install_result_guard(Store)
    again = install_result_guard(Store)
    valid = asyncio.get_running_loop().create_future()
    canceled = asyncio.get_running_loop().create_future()
    canceled.cancel()
    Store._futures = {1: valid, 2: canceled}
    first = {"echo": {"seq": 1}, "data": "valid"}
    Store.add(first)
    Store.add({"echo": {"seq": 1}, "data": "duplicate"})
    Store.add({"echo": {"seq": 2}, "data": "late"})
    Store.add({"echo": {"seq": 999}, "data": "expired"})
    assert await valid == first
    assert canceled.cancelled()
    assert Store._niuniu_ignored_results == 2
    again()
    assert Store._niuniu_result_guard
    cleanup()
    assert Store.__dict__["add"] is original
    assert not Store._niuniu_result_guard
