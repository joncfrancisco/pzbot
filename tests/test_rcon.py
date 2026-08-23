"""The RCON client, against a real socket.

Framing bugs are the kind that only show up under load -- a truncated read, a packet
boundary in the wrong place -- so these tests speak the actual protocol over a real
loopback connection rather than mocking the socket away.
"""

from __future__ import annotations

import asyncio
import struct
from contextlib import asynccontextmanager

import pytest

from pzbot import rcon as r


def test_pack_unpack_roundtrip():
    packet = r.pack(7, r.SERVERDATA_EXECCOMMAND, "players")
    size = struct.unpack("<i", packet[:4])[0]
    assert size == len(packet) - 4
    assert r.unpack(packet[4:]) == (7, r.SERVERDATA_EXECCOMMAND, "players")


def test_parse_players_and_count():
    output = "Players connected (2):\n-Bob\n-Alice"
    assert r.parse_players(output) == ["Bob", "Alice"]
    assert r.parse_player_count(output) == 2
    assert r.looks_ready(output)


def test_parse_empty_server():
    output = "Players connected (0):"
    assert r.parse_players(output) == []
    assert r.parse_player_count(output) == 0
    assert r.looks_ready(output)


def test_a_socket_that_answers_nonsense_is_not_ready():
    # PZ accepts RCON connections before the world has loaded. "The port is open" must
    # never be mistaken for "you can connect" -- see DESIGN section 8.
    assert not r.looks_ready("")
    assert not r.looks_ready("Unknown command")


@asynccontextmanager
async def serving(handler):
    """A loopback RCON server for the duration of a `with` block.

    `wait_closed()` is deliberately not awaited on the way out: since Python 3.12 it
    blocks until every accepted connection has gone away, and a test whose whole point
    is a server that never answers would hang there forever.
    """
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield host, port
    finally:
        server.close()


async def _read_packet(reader):
    size = struct.unpack("<i", await reader.readexactly(4))[0]
    return r.unpack(await reader.readexactly(size))


async def test_execute_against_a_real_server():
    async def handler(reader, writer):
        req_id, _, password = await _read_packet(reader)
        assert password == "hunter2"
        writer.write(r.pack(req_id, r.SERVERDATA_AUTH_RESPONSE, ""))
        _, _, command = await _read_packet(reader)
        writer.write(r.pack(2, r.SERVERDATA_RESPONSE_VALUE, f"Players connected (1):\n-{command}"))
        await writer.drain()
        writer.close()

    async with serving(handler) as (host, port):
        out = await r.Rcon(host, port, "hunter2").execute("players", timeout=5)
    assert out == "Players connected (1):\n-players"


async def test_a_long_response_is_reassembled_across_packets():
    # A busy server's `players` list arrives in more than one packet. Truncating it would
    # make the bot under-report who is online, which is exactly the number the idle
    # watchdog's whole cost guarantee is built on.
    names = [f"survivor{i:03d}" for i in range(200)]
    body = "Players connected (200):" + "".join(f"\n-{n}" for n in names)

    async def handler(reader, writer):
        req_id, _, _ = await _read_packet(reader)
        writer.write(r.pack(req_id, r.SERVERDATA_AUTH_RESPONSE, ""))
        await _read_packet(reader)
        for chunk in (body[:3600], body[3600:]):
            writer.write(r.pack(2, r.SERVERDATA_RESPONSE_VALUE, chunk))
        await writer.drain()
        writer.close()

    async with serving(handler) as (host, port):
        out = await r.Rcon(host, port, "hunter2").execute("players", timeout=5)
    assert r.parse_player_count(out) == 200
    assert r.parse_players(out) == names


async def test_bad_password_raises_auth_error():
    async def handler(reader, writer):
        await _read_packet(reader)
        # The protocol signals rejection with request id -1, not an error message.
        writer.write(r.pack(-1, r.SERVERDATA_AUTH_RESPONSE, ""))
        await writer.drain()
        writer.close()

    async with serving(handler) as (host, port):
        with pytest.raises(r.RconAuthError):
            await r.Rcon(host, port, "wrong").execute("players", timeout=5)


async def test_closed_port_is_unreachable_not_an_error():
    # A stopped game server looks exactly like this, and it must read as "not ready"
    # rather than as a failure -- Stage.BOOTING, not Stage.UNKNOWN.
    async with serving(lambda *_: None) as (host, port):
        pass
    with pytest.raises(r.RconUnreachable):
        await r.Rcon(host, port, "hunter2").execute("players", timeout=1)


async def test_a_server_that_accepts_and_never_answers_times_out():
    # PZ opens the RCON port minutes before the world has finished loading, so this is
    # not a hypothetical: it is what every `/pz start` sees for the first few minutes.
    async def handler(reader, writer):
        await asyncio.Event().wait()

    async with serving(handler) as (host, port):
        with pytest.raises(r.RconUnreachable):
            await r.Rcon(host, port, "hunter2").execute("players", timeout=0.5)
