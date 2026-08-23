"""Who did what, when, and whether it worked.

pzserver DESIGN section 10: every state-changing command is logged to an audit channel.
The reason is less about blame than about correlation -- when someone asks "why did the
server go down at 9pm", the answer is either in this channel (a person did it) or it is
not (the idle watchdog or a budget alarm did it), and that distinction is the first fork
in every investigation.

Nothing in here is allowed to raise. An audit channel that has been deleted, or that the
bot has lost permission to post in, must not turn a successful `/pz start` into a failed
one.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord

log = logging.getLogger(__name__)

OK = discord.Colour(0x3BA55D)
FAIL = discord.Colour(0xED4245)
INFO = discord.Colour(0x4F545C)


class Audit:
    def __init__(self, client: discord.Client, channel_id: int) -> None:
        self._client = client
        self._channel_id = channel_id

    async def record(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        outcome: str = "ok",
        detail: str = "",
    ) -> None:
        user = interaction.user
        # The structured log line is the real audit trail; Discord is the convenient one.
        # journald survives a channel being deleted, and it is what `journalctl -u pzbot`
        # shows when someone is already SSM'd into the box.
        log.info(
            "audit action=%s outcome=%s user=%s(%s) channel=%s detail=%s",
            action,
            outcome,
            user,
            user.id,
            interaction.channel_id,
            detail.replace("\n", " ")[:300],
        )
        if not self._channel_id:
            return

        embed = discord.Embed(
            description=f"**`/pz {action}`** — {outcome}",
            colour={"ok": OK, "failed": FAIL, "denied": FAIL}.get(outcome, INFO),
            timestamp=dt.datetime.now(dt.UTC),
        )
        embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        if detail:
            embed.add_field(name="Detail", value=detail[:1000])
        if interaction.channel_id:
            embed.set_footer(text=f"in #{getattr(interaction.channel, 'name', '?')}")

        try:
            channel = self._client.get_channel(
                self._channel_id
            ) or await self._client.fetch_channel(self._channel_id)
            await channel.send(embed=embed)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 -- see module docstring
            log.exception("could not write to the audit channel %s", self._channel_id)
