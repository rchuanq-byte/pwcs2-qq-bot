"""Legacy gateway adapter kept for rollback; never imported by the AstrBot runtime."""

from nanobot.channels.napcat import NapcatChannel

from pwcs2_bot.channel import PWCS2Application


class PWCS2Channel(PWCS2Application, NapcatChannel):
    """Original nanobot transport around the shared application."""
