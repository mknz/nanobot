"""Tests for the Delta Chat channel."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.deltachat import DeltaChatChannel, DeltaChatConfig


def _make_config(**overrides: Any) -> DeltaChatConfig:
    defaults: dict[str, Any] = dict(
        enabled=True,
        addr="bot@nine.testrun.org",
        password="secret",
        accounts_dir="/tmp/deltachat-test",
        display_name="testbot",
        allow_from=["*"],
    )
    defaults.update(overrides)
    return DeltaChatConfig(**defaults)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _make_channel(bus: MagicMock, **kw: Any) -> DeltaChatChannel:
    config = _make_config(**kw)
    return DeltaChatChannel(config, bus)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_default_config_keys() -> None:
    cfg = DeltaChatChannel.default_config()
    assert "enabled" in cfg or "enabled" in str(cfg)
    assert not cfg.get("enabled", True)  # default is False


def test_config_from_dict() -> None:
    ch = DeltaChatChannel(
        {"addr": "bot@example.com", "password": "pw", "allowFrom": ["*"]},
        MagicMock(),
    )
    assert ch.config.addr == "bot@example.com"
    assert ch.config.allow_from == ["*"]


def test_config_camel_case() -> None:
    cfg = DeltaChatConfig.model_validate({"displayName": "mybot", "allowFrom": ["alice@example.com"]})
    assert cfg.display_name == "mybot"
    assert cfg.allow_from == ["alice@example.com"]


# ---------------------------------------------------------------------------
# start() — missing credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_missing_addr_returns_early(bus: MagicMock) -> None:
    ch = DeltaChatChannel(_make_config(addr=""), bus)
    await ch.start()  # should return without error
    bus.publish_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_start_missing_password_returns_early(bus: MagicMock) -> None:
    ch = DeltaChatChannel(_make_config(password=""), bus)
    await ch.start()
    bus.publish_inbound.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for mocking deltachat2
# ---------------------------------------------------------------------------


def _make_fake_deltachat2(events: list[Any], *, configured: bool = True) -> tuple[MagicMock, MagicMock]:
    """Return (mock_module, mock_rpc) with canned event sequence."""

    event_iter = iter(events + [StopIteration])

    def get_next_event() -> Any:
        val = next(event_iter)
        if val is StopIteration:
            raise Exception("stop")
        return val

    rpc = MagicMock()
    rpc.get_all_account_ids.return_value = [1]
    rpc.is_configured.return_value = configured
    rpc.get_next_event.side_effect = get_next_event
    rpc.get_next_msgs.return_value = []
    rpc.get_chat_securejoin_qr_code.return_value = "OPENPGP4FPR:test"

    transport = MagicMock()

    fake_mod = MagicMock()
    fake_mod.IOTransport.return_value = transport
    fake_mod.Rpc.return_value = rpc

    return fake_mod, rpc


def _incoming_event(acc: int = 1) -> SimpleNamespace:
    """Build a fake INCOMING_MSG_BUNCH event."""
    from deltachat2 import EventType  # type: ignore[import]
    event = SimpleNamespace(kind=EventType.INCOMING_MSG_BUNCH)
    return SimpleNamespace(event=event, context_id=acc)


def _other_event(acc: int = 1) -> SimpleNamespace:
    from deltachat2 import EventType  # type: ignore[import]
    event = SimpleNamespace(kind=EventType.INFO)
    return SimpleNamespace(event=event, context_id=acc)


# ---------------------------------------------------------------------------
# send() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_not_connected_skips(bus: MagicMock) -> None:
    ch = _make_channel(bus)
    # _rpc is None — send should return without raising
    msg = OutboundMessage(channel="deltachat", chat_id="5", content="hi")
    await ch.send(msg)  # no exception


@pytest.mark.asyncio
async def test_send_invalid_chat_id_skips(bus: MagicMock) -> None:
    ch = _make_channel(bus)
    ch._rpc = MagicMock()
    ch._account_id = 1
    msg = OutboundMessage(channel="deltachat", chat_id="not-an-int", content="hi")
    await ch.send(msg)  # no exception; rpc.send_msg must NOT be called
    ch._rpc.send_msg.assert_not_called()


@pytest.mark.asyncio
async def test_send_calls_rpc_send_msg(bus: MagicMock) -> None:
    ch = _make_channel(bus)
    rpc = MagicMock()
    ch._rpc = rpc
    ch._account_id = 1

    try:
        from deltachat2 import MsgData  # type: ignore[import]
    except ImportError:
        pytest.skip("deltachat2 not installed")

    msg = OutboundMessage(channel="deltachat", chat_id="7", content="hello")
    await ch.send(msg)

    rpc.send_msg.assert_called_once()
    call_args = rpc.send_msg.call_args
    assert call_args[0][0] == 1   # account_id
    assert call_args[0][1] == 7   # chat_id


# ---------------------------------------------------------------------------
# is_allowed() integration
# ---------------------------------------------------------------------------


def test_is_allowed_wildcard(bus: MagicMock) -> None:
    ch = _make_channel(bus, allow_from=["*"])
    assert ch.is_allowed("anyone@example.com")


def test_is_allowed_empty_denies_all(bus: MagicMock) -> None:
    ch = _make_channel(bus, allow_from=[])
    assert not ch.is_allowed("anyone@example.com")


def test_is_allowed_specific(bus: MagicMock) -> None:
    ch = _make_channel(bus, allow_from=["alice@example.com"])
    assert ch.is_allowed("alice@example.com")
    assert not ch.is_allowed("bob@example.com")


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sets_running_false(bus: MagicMock) -> None:
    ch = _make_channel(bus)
    ch._running = True
    await ch.stop()
    assert not ch._running


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------


def test_channel_is_discoverable() -> None:
    from nanobot.channels.registry import discover_all
    try:
        channels = discover_all()
        assert "deltachat" in channels
    except ImportError:
        pytest.skip("deltachat2 not installed; registry skips it")
