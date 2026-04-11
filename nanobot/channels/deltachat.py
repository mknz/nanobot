"""Delta Chat channel — email-based encrypted messaging via deltachat2."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class DeltaChatConfig(Base):
    """Delta Chat channel configuration."""

    enabled: bool = False
    addr: str = ""
    password: str = ""
    accounts_dir: str = "~/.nanobot/deltachat"
    display_name: str = "nanobot"
    allow_from: list[str] = Field(default_factory=list)


class DeltaChatChannel(BaseChannel):
    """
    Delta Chat channel.

    Inbound:
    - Event-driven: listens for INCOMING_MSG_BUNCH events via deltachat2 RPC.

    Outbound:
    - Sends replies via rpc.send_msg().
    """

    name = "deltachat"
    display_name = "Delta Chat"

    def __init__(self, config: Any, bus: MessageBus) -> None:
        if isinstance(config, dict):
            config = DeltaChatConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: DeltaChatConfig = config
        self._rpc: Any = None
        self._transport: Any = None
        self._account_id: int | None = None

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return DeltaChatConfig().model_dump(by_alias=True)

    def _setup(self) -> tuple[Any, Any, int]:
        """Blocking setup: initialise RPC, configure account, start IO."""
        from deltachat2 import IOTransport, Rpc

        state_dir = Path(self.config.accounts_dir).expanduser()
        # Create the *parent* only — deltachat-rpc-server creates state_dir itself.
        state_dir.parent.mkdir(parents=True, exist_ok=True)

        transport = IOTransport(accounts_dir=str(state_dir))
        transport.start()
        rpc = Rpc(transport)

        accounts = rpc.get_all_account_ids()
        account_id: int = accounts[0] if accounts else rpc.add_account()

        rpc.set_config(account_id, "bot", "1")
        if self.config.display_name:
            rpc.set_config(account_id, "displayname", self.config.display_name)

        if not rpc.is_configured(account_id):
            rpc.add_or_update_transport(
                account_id,
                {"addr": self.config.addr, "password": self.config.password},
            )

        # Force keypair generation before start_io to enable decryption of
        # incoming messages from the first contact.
        with tempfile.TemporaryDirectory() as tmp:
            rpc.export_self_keys(account_id, tmp, None)

        rpc.start_io(account_id)

        try:
            invite = rpc.get_chat_securejoin_qr_code(account_id, None)
            logger.info("Delta Chat invite link (share for first contact): {}", invite)
        except Exception:
            pass

        return rpc, transport, account_id

    async def start(self) -> None:
        """Connect to Delta Chat and listen for incoming messages."""
        if not self.config.addr or not self.config.password:
            logger.error("Delta Chat channel not configured: addr and password required")
            return

        logger.info("Starting Delta Chat channel as {} ...", self.config.addr)

        rpc, transport, account_id = await asyncio.to_thread(self._setup)
        self._rpc = rpc
        self._transport = transport
        self._account_id = account_id
        self._running = True

        logger.info("Delta Chat channel ready.")

        try:
            await self._event_loop()
        finally:
            self._running = False
            await self._teardown()

    async def _event_loop(self) -> None:
        from deltachat2 import EventType

        rpc = self._rpc
        account_id = self._account_id

        while self._running:
            try:
                raw = await asyncio.to_thread(rpc.get_next_event)
            except Exception as exc:
                if self._running:
                    logger.warning("Delta Chat event loop error: {}", exc)
                break

            if raw is None:
                continue

            if raw.event.kind != EventType.INCOMING_MSG_BUNCH:
                continue

            acc = raw.context_id
            try:
                msg_ids = await asyncio.to_thread(rpc.get_next_msgs, acc)
            except Exception:
                try:
                    msg_ids = await asyncio.to_thread(rpc.get_fresh_msgs, acc)
                except Exception as exc:
                    logger.warning("Delta Chat: failed to get messages: {}", exc)
                    continue

            seen: list[int] = []
            for msg_id in msg_ids:
                try:
                    msg = await asyncio.to_thread(rpc.get_message, acc, msg_id)
                except Exception as exc:
                    logger.warning("Delta Chat: failed to fetch msg {}: {}", msg_id, exc)
                    continue

                text: str = getattr(msg, "text", "") or ""
                if not text:
                    continue

                chat_id = str(getattr(msg, "chat_id", acc))
                sender = getattr(msg, "sender", acc)
                sender_id = str(getattr(sender, "address", acc))

                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=text,
                    metadata={"account_id": acc, "msg_id": msg_id},
                )
                seen.append(msg_id)

            if seen:
                try:
                    await asyncio.to_thread(rpc.markseen_msgs, acc, seen)
                except Exception:
                    pass

    async def _teardown(self) -> None:
        if self._rpc is not None and self._account_id is not None:
            try:
                await asyncio.to_thread(self._rpc.stop_io, self._account_id)
            except Exception:
                pass
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._rpc = None
        self._transport = None
        self._account_id = None

    async def stop(self) -> None:
        """Signal the event loop to stop."""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply to a Delta Chat chat."""
        if self._rpc is None or self._account_id is None:
            logger.warning("Delta Chat channel not connected, cannot send")
            return

        chat_id_raw = msg.chat_id
        try:
            chat_id = int(chat_id_raw)
        except (ValueError, TypeError):
            logger.warning("Delta Chat: invalid chat_id: {}", chat_id_raw)
            return

        from deltachat2 import MsgData

        data = MsgData(text=msg.content or "")
        try:
            await asyncio.to_thread(self._rpc.send_msg, self._account_id, chat_id, data)
        except Exception as exc:
            logger.error("Delta Chat send error to chat {}: {}", chat_id, exc)
            raise
