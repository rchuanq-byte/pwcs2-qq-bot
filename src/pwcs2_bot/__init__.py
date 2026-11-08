"""Perfect World CS2 QQ bot plugin for nanobot."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pwcs2_bot.channel import PWCS2Channel

__all__ = ["PWCS2Channel"]


def __getattr__(name: str):
    # Readiness probes run in short-lived processes. Importing the entire bot,
    # all integrations and Nanobot just to read a tiny JSON snapshot wastes
    # memory on every probe. Preserve the public export, loading it on demand.
    if name == "PWCS2Channel":
        from pwcs2_bot.channel import PWCS2Channel

        globals()[name] = PWCS2Channel
        return PWCS2Channel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
