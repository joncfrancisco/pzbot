"""Shared machinery for the command modules: context, progress messages, replies."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

import discord

from .. import render
from ..audit import Audit
from ..aws import Aws
from ..config import Config
from ..server import GameServer
from ..singleflight import Busy, SingleFlight

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    """Everything a command needs, assembled once at startup."""

    cfg: Config
    aws: Aws
    server: GameServer
    lock: SingleFlight
    audit: Audit

    # Cost Explorer bills per request and lags a day; the backup listing is only used
    # for autocomplete. Neither is server state, so neither is subject to the
    # never-cache rule.
    spend: Cached = field(default_factory=lambda: Cached(dt.timedelta(hours=6)))
    backups: Cached = field(default_factory=lambda: Cached(dt.timedelta(seconds=60)))


async def reply(
    interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = False
) -> None:
    """Answer an interaction whether or not it has already been deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


class Live:
    """A message that edits itself while a long operation runs.

    pzserver DESIGN section 10 asks for exactly this: defer immediately, post one embed,
    then edit *that* message every few seconds rather than spamming the channel. A start
    takes three to seven minutes, and a channel with twenty "still loading…" messages in
    it is worse than no feedback at all.

    Edits are throttled: identical text inside the throttle window is dropped, because
    the poll loop calls this on a fixed interval whether or not anything changed.
    """

    THROTTLE = dt.timedelta(seconds=4)

    def __init__(self, interaction: discord.Interaction, title: str) -> None:
        self._interaction = interaction
        self._title = title
        self._message: discord.WebhookMessage | None = None
        self._last_line = ""
        self._last_edit = dt.datetime.now(dt.UTC) - self.THROTTLE
        self.started = dt.datetime.now(dt.UTC)

    async def open(self, line: str) -> None:
        if not self._interaction.response.is_done():
            await self._interaction.response.defer()
        self._message = await self._interaction.followup.send(
            embed=render.progress(self._title, line, self.started), wait=True
        )
        self._last_line = line

    async def __call__(self, line: str) -> None:
        if self._message is None:
            await self.open(line)
            return
        now = dt.datetime.now(dt.UTC)
        if line == self._last_line and now - self._last_edit < self.THROTTLE:
            return
        self._last_line = line
        self._last_edit = now
        try:
            await self._message.edit(embed=render.progress(self._title, line, self.started))
        except discord.HTTPException as exc:
            # A dropped progress edit must never fail the operation behind it. The final
            # message is what matters, and that one is retried by `finish`.
            log.warning("could not edit the progress message: %s", exc)

    async def finish(self, embed: discord.Embed, *, content: str | None = None) -> None:
        if self._message is None:
            await self._interaction.followup.send(content=content or "", embed=embed)
            return
        try:
            await self._message.edit(content=content, embed=embed)
        except discord.HTTPException:
            await self._interaction.followup.send(content=content or "", embed=embed)


class Cached:
    """A one-value TTL cache, for answers that cost money or rate limit.

    Deliberately *not* used for server state -- pzserver DESIGN section 10 forbids
    caching that, and for good reason. This exists for Cost Explorer, which charges a
    cent per request and reports data that is up to a day stale anyway, and for the S3
    listing behind `/pz restore`'s autocomplete, which would otherwise be re-listed on
    every keystroke.
    """

    def __init__(self, ttl: dt.timedelta) -> None:
        self._ttl = ttl
        self._value = None
        self._fetched: dt.datetime | None = None
        self._lock = asyncio.Lock()

    async def get(self, factory):
        async with self._lock:  # one fetch, however many callers are waiting
            now = dt.datetime.now(dt.UTC)
            if self._fetched is None or now - self._fetched > self._ttl:
                self._value = await factory()
                self._fetched = now
            return self._value

    def invalidate(self) -> None:
        self._fetched = None


class Confirm(discord.ui.View):
    """Two-step confirmation, bound to the person who asked.

    The `user_id` check is not paranoia about attackers -- it is about the far more
    likely accident of someone else in the channel clicking the red button on a
    confirmation they did not read.

    Buttons are built in `__init__` rather than declared with `@discord.ui.button`,
    because the label is the whole point: "Restore the world" and "Switch to b41multiplayer"
    are different promises, and a view that says `Confirm` for both is a view people click
    without reading.
    """

    def __init__(
        self,
        user_id: int,
        label: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.danger,
    ) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.value = False

        go = discord.ui.Button(label=label, style=style)
        go.callback = self._confirm
        back_out = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        back_out.callback = self._cancel
        self.add_item(go)
        self.add_item(back_out)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This confirmation belongs to whoever ran the command.", ephemeral=True
            )
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirmed(
    interaction: discord.Interaction,
    embed: discord.Embed,
    *,
    label: str,
    title: str = "Cancelled",
    cancelled: str = "Nothing was changed.",
) -> bool:
    """Put a decision in front of someone and wait for it. False means: do nothing.

    Every command that reaches this point is one that can lose a world -- a restore, a
    branch switch, a mod change. They all owe the same thing: say what is about to happen
    in full BEFORE the button, and leave a message behind saying nothing happened when the
    button is not pressed. A confirmation that times out silently reads as one that was
    accepted.
    """
    view = Confirm(interaction.user.id, label)
    embed.set_footer(text="This confirmation expires in 60 seconds.")
    await interaction.response.send_message(embed=embed, view=view)
    await view.wait()

    if view.value:
        return True

    await interaction.edit_original_response(
        embed=discord.Embed(title=title, description=cancelled, colour=render.OFF),
        view=None,
    )
    return False


def busy_embed(busy: Busy) -> discord.Embed:
    """Rejected, not queued. DESIGN section 10 is explicit about the difference."""
    holder = busy.holder
    return render.error(
        f"**{holder.who}** started a `{holder.operation}` {holder.age_seconds}s ago and it is "
        "still running. Wait for it to finish rather than stacking another operation on top "
        "\N{EM DASH} `/pz status` will show you where it got to.",
        title="Something else is already happening",
    )
