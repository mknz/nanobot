"""Minimal Delta Chat echo bot — demonstrates the full integration pattern."""

import argparse
import asyncio
import tempfile
from pathlib import Path


async def run_bot(addr: str, password: str, accounts_dir: str) -> None:
    from deltachat2 import EventType, IOTransport, MsgData, Rpc

    state_dir = Path(accounts_dir).expanduser()
    # Create the *parent* only — deltachat-rpc-server initialises accounts_dir
    # itself (including accounts.toml). If accounts_dir already exists but has
    # no accounts.toml, the binary errors.
    state_dir.parent.mkdir(parents=True, exist_ok=True)

    def setup():
        transport = IOTransport(accounts_dir=str(state_dir))
        transport.start()
        rpc = Rpc(transport)

        accounts = rpc.get_all_account_ids()
        account_id = accounts[0] if accounts else rpc.add_account()

        rpc.set_config(account_id, "bot", "1")
        rpc.set_config(account_id, "displayname", "nanobot-deltachat")

        if not rpc.is_configured(account_id):
            rpc.add_or_update_transport(
                account_id, {"addr": addr, "password": password}
            )

        # Force keypair generation before start_io — deltachat-core generates
        # keys lazily on first send_msg(), so incoming messages can't be
        # decrypted until we trigger it here.
        with tempfile.TemporaryDirectory() as tmp:
            rpc.export_self_keys(account_id, tmp, None)

        rpc.start_io(account_id)

        # Log the securejoin invite link for first-contact bootstrapping.
        # On chatmail servers, contacts must scan this to establish encryption.
        try:
            invite = rpc.get_chat_securejoin_qr_code(account_id, None)
            print(f"Invite link (share this for first contact): {invite}")
        except Exception:
            pass

        return rpc, transport, account_id

    print(f"Connecting as {addr} ...")
    rpc, transport, account_id = await asyncio.to_thread(setup)
    print("Ready. Waiting for messages...")

    try:
        while True:
            raw = await asyncio.to_thread(rpc.get_next_event)
            if raw is None:
                continue

            kind = raw.event.kind
            acc = raw.context_id

            if kind == EventType.INCOMING_MSG_BUNCH:
                try:
                    msg_ids = await asyncio.to_thread(rpc.get_next_msgs, acc)
                except Exception:
                    msg_ids = await asyncio.to_thread(rpc.get_fresh_msgs, acc)

                for msg_id in msg_ids:
                    try:
                        msg = await asyncio.to_thread(rpc.get_message, acc, msg_id)
                    except Exception as e:
                        print(f"Error fetching msg {msg_id}: {e}")
                        continue

                    text = getattr(msg, "text", "") or ""
                    if not text:
                        continue

                    chat_id = getattr(msg, "chat_id", None)
                    print(f"Message in chat {chat_id}: {text!r}")

                    reply = MsgData(text=f"Echo: {text}")
                    try:
                        await asyncio.to_thread(rpc.send_msg, acc, chat_id, reply)
                    except Exception as e:
                        print(f"Error sending reply: {e}")

                if msg_ids:
                    try:
                        await asyncio.to_thread(rpc.markseen_msgs, acc, msg_ids)
                    except Exception:
                        pass

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try:
            await asyncio.to_thread(rpc.stop_io, account_id)
        except Exception:
            pass
        try:
            transport.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Delta Chat echo bot")
    parser.add_argument("--addr", required=True, help="Bot email address")
    parser.add_argument("--password", required=True, help="Email password")
    parser.add_argument(
        "--accounts-dir",
        default="~/.deltachat-bot",
        help="State directory (default: ~/.deltachat-bot)",
    )
    args = parser.parse_args()
    asyncio.run(run_bot(args.addr, args.password, args.accounts_dir))


if __name__ == "__main__":
    main()
