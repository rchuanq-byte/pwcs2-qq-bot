"""Compatibility for aiocqhttp's already-completed reverse-WS API futures."""

from __future__ import annotations


def install_result_guard(store):
    """Ignore late/duplicate echoes, never crash the entire OneBot socket.

    aiocqhttp ResultStore.add unconditionally calls future.set_result. A second
    reply, or one arriving between cancellation and fetch's finally cleanup,
    raises InvalidStateError inside the universal websocket's receive loop.
    Preserve the first valid result and all existing timeout/error semantics.
    """
    if getattr(store, "_niuniu_result_guard", False):
        return lambda: None
    original = store.__dict__["add"]
    store._niuniu_ignored_results = 0

    def guarded(cls, result):
        echo = result.get("echo") if isinstance(result, dict) else None
        if isinstance(echo, dict) and isinstance(echo.get("seq"), int):
            future = cls._futures.get(echo["seq"])
            if future is not None and future.done():
                cls._niuniu_ignored_results += 1
                return
        return original.__func__(cls, result)

    store.add = classmethod(guarded)
    store._niuniu_result_guard = True

    def cleanup():
        if getattr(store.add, "__func__", None) is guarded:
            store.add = original
            store._niuniu_result_guard = False

    return cleanup
