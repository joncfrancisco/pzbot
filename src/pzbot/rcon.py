"""Source RCON, asyncio, vendored.

This is a port of `pzserver`'s `ops/bin/pz-rcon` to asyncio, and it is vendored for the
same reason that one is: the protocol is sixty lines, and every readiness check, every
save and every graceful stop in this bot goes through it. A vendored copy cannot break
because a small package went unmaintained.

Two deliberate choices:

*   **A fresh connection per command.** RCON is a single-socket request/response
    protocol with no keepalive, and the server it talks to is powered off most of the
    time. A pooled connection would spend its life in one of two states -- stale or
    reconnecting -- and "is the socket still good?" is exactly the cached state DESIGN
    section 10 forbids. Connecting costs a few milliseconds against a game that takes
    minutes to boot.
*   **Failure is typed.** `RconUnreachable` (the box is off, or PZ has not opened the
    port yet) means "not ready" and is a normal, expected answer during a start.
    `RconAuthError` means the password in Parameter Store no longer matches the one in
    the .ini, which is an operator problem and must never be reported as "not ready".
"""

from __future__ import annotations

import asyncio
import re
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

_MAX_PACKET = 4_194_304


class RconError(Exception):
    """Protocol-level failure: a malformed or implausible packet."""


class RconAuthError(RconError):
    """The password was rejected. An operator problem, never a readiness problem."""


class RconUnreachable(RconError):
    """Could not connect, or the peer went away. Expected while the world is loading."""


def pack(req_id: int, req_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def unpack(payload: bytes) -> tuple[int, int, str]:
    req_id, req_type = struct.unpack("<ii", payload[:8])
    body = payload[8:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return req_id, req_type, body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    header = await reader.readexactly(4)
    size = struct.unpack("<i", header)[0]
    if size < 10 or size > _MAX_PACKET:
        raise RconError(f"implausible packet size {size}")
    return unpack(await reader.readexactly(size))


class Rcon:
    """A single game server's RCON endpoint."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self._password = password
        self.timeout = timeout

    async def execute(self, command: str, *, timeout: float | None = None) -> str:
        timeout = timeout or self.timeout
        try:
            return await asyncio.wait_for(self._execute(command, timeout), timeout + 5)
        except TimeoutError as exc:
            raise RconUnreachable(f"{self.host}:{self.port} did not answer in time") from exc

    async def _execute(self, command: str, timeout: float) -> str:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout
            )
        except (OSError, TimeoutError) as exc:
            raise RconUnreachable(f"cannot reach {self.host}:{self.port}: {exc}") from exc

        try:
            writer.write(pack(1, SERVERDATA_AUTH, self._password))
            await writer.drain()

            # Some implementations emit an empty RESPONSE_VALUE before the auth verdict.
            # Skip anything that is not the verdict itself.
            #
            # Bounded by `timeout` rather than left to the outer guard, because a server
            # that accepts the connection and then says nothing is the normal state of a
            # PZ server that is still loading its world -- the bot sees it on every
            # start, and it should cost one timeout, not two.
            async def authenticate() -> int:
                while True:
                    req_id, req_type, _ = await _read_packet(reader)
                    if req_type == SERVERDATA_AUTH_RESPONSE:
                        return req_id

            try:
                req_id = await asyncio.wait_for(authenticate(), timeout)
            except TimeoutError as exc:
                raise RconUnreachable(
                    f"{self.host}:{self.port} accepted the connection but never "
                    "answered the auth request"
                ) from exc
            if req_id == -1:
                raise RconAuthError(
                    "RCON authentication failed -- the password in Parameter Store does "
                    "not match the one the server booted with"
                )

            writer.write(pack(2, SERVERDATA_EXECCOMMAND, command))
            await writer.drain()

            # PZ answers in one packet in practice; drain until quiet anyway so a long
            # `players` list on a busy server is never truncated.
            parts: list[str] = []
            wait = timeout
            while True:
                try:
                    _, req_type, body = await asyncio.wait_for(_read_packet(reader), wait)
                except (TimeoutError, asyncio.IncompleteReadError):
                    break
                if req_type == SERVERDATA_RESPONSE_VALUE:
                    parts.append(body)
                if len(body) < 3500:  # short packet: the response is complete
                    wait = 0.4  # brief drain for a possible trailer, then stop
            return "".join(parts).strip()
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            raise RconUnreachable(f"connection to {self.host}:{self.port} dropped: {exc}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass


_COUNT = re.compile(r"\((\d+)\)")


def parse_players(output: str) -> list[str]:
    """Names out of PZ's `players` response.

    The response looks like::

        Players connected (2):
        -Bob
        -Alice

    The header count is authoritative for *how many*; the dashed lines are parsed for
    names but a name list that disagrees with the header is not treated as an error --
    a player connecting mid-response should not make `/pz who` fail.
    """
    names = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("-") and len(line) > 1:
            names.append(line[1:].strip())
    return [n for n in names if n]


def parse_player_count(output: str) -> int:
    match = _COUNT.search(output)
    if match:
        return int(match.group(1))
    return len(parse_players(output))


def looks_ready(output: str) -> bool:
    """DESIGN section 8: readiness is `players` answering with a well-formed response.

    "The socket accepted a connection" is not enough -- PZ opens the RCON port before
    the world has finished loading, and a bot that announces "ready" on a bare TCP
    connect sends six people to a server that is still counting zombies.
    """
    return bool(_COUNT.search(output)) or "Players connected" in output
