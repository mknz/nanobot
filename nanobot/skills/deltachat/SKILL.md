---
name: deltachat
description: "Add Delta Chat bot support to a Python project. Delta Chat is an email-based IM app with automatic encryption. Use this skill when asked to integrate Delta Chat messaging, create a Delta Chat bot, or send/receive Delta Chat messages."
metadata: {"nanobot":{"emoji":"💬","requires":{"bins":["deltachat-rpc-server"],"packages":["deltachat2>=0.9.0","deltachat-rpc-server>=2.42.0"]}}}
---

# Delta Chat Bot Integration

[Delta Chat](https://delta.chat) is an instant-messenger built on email — any email address works as an account, and messages are end-to-end encrypted automatically via Autocrypt.

## Install

```bash
pip install "deltachat2>=0.9.0" "deltachat-rpc-server>=2.42.0"
# or with uv:
uv add "deltachat2>=0.9.0" "deltachat-rpc-server>=2.42.0"
```

`deltachat-rpc-server` is a Rust binary bundled as a Python wheel. `deltachat2` is the Python JSON-RPC client that communicates with it.

## Quick-start script

Run `{baseDir}/scripts/bot.py` to start a minimal echo bot:

```bash
python {baseDir}/scripts/bot.py --addr mybot@nine.testrun.org --password mypassword
```

## Chatmail servers (zero-setup accounts)

[Chatmail servers](https://delta.chat/chatmail) (e.g. `nine.testrun.org`, `c3.email`) let you create an account by simply picking any unused address and password — no registration required. Accounts are created on first login.

```
addr:     mybot@nine.testrun.org
password: any-string-you-choose
```

## API overview

```python
from deltachat2 import IOTransport, Rpc, EventType, MsgData

# 1. Start the RPC server subprocess
transport = IOTransport(accounts_dir="/path/to/state")
transport.start()
rpc = Rpc(transport)

# 2. Create or reuse an account
accounts = rpc.get_all_account_ids()
account_id = accounts[0] if accounts else rpc.add_account()

# 3. Configure (only needed on first run)
rpc.set_config(account_id, "bot", "1")
rpc.set_config(account_id, "displayname", "My Bot")
if not rpc.is_configured(account_id):
    rpc.add_or_update_transport(
        account_id,
        {"addr": ADDR, "password": PASSWORD},
    )

# 4. Force keypair generation BEFORE start_io (see gotchas below)
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    rpc.export_self_keys(account_id, tmp, None)

# 5. Start IO (connects to the email server)
rpc.start_io(account_id)

# 6. Event loop — wait for incoming messages
while True:
    raw = rpc.get_next_event()          # blocking
    if raw.event.kind == EventType.INCOMING_MSG_BUNCH:
        for msg_id in rpc.get_next_msgs(account_id):
            msg = rpc.get_message(account_id, msg_id)
            print(msg.text)
            # reply
            rpc.send_msg(account_id, msg.chat_id,
                         MsgData(text=f"Echo: {msg.text}"))
        rpc.markseen_msgs(account_id, list(rpc.get_next_msgs(account_id)))

# 7. Clean up
rpc.stop_io(account_id)
transport.close()
```

## Configuration schema (Pydantic)

```python
from pydantic import BaseModel, Field

class DeltaChatConfig(BaseModel):
    enabled: bool = False
    addr: str = ""           # Bot email address
    password: str = ""       # Email password (app password recommended)
    accounts_dir: str = ""   # State dir (default: ~/.myapp/deltachat)
    display_name: str = "Bot"
    allow_from: list[str] = Field(default_factory=list)  # Empty = accept all
```

## Async usage

All `rpc.*` calls are blocking. Wrap with `asyncio.to_thread()`:

```python
import asyncio

msg = await asyncio.to_thread(rpc.get_message, account_id, msg_id)
await asyncio.to_thread(rpc.send_msg, account_id, chat_id, MsgData(text="hello"))
raw_event = await asyncio.to_thread(rpc.get_next_event)
```

## Gotchas

### 1. Lazy keypair generation (critical)

`deltachat-core` generates the Autocrypt keypair lazily on the **first `send_msg()`**, not during `add_or_update_transport()` or `start_io()`. This means the bot cannot decrypt incoming messages until it has sent at least one message.

**Fix**: force generation immediately after configuring, before `start_io()`:

```python
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    rpc.export_self_keys(account_id, tmp, None)
```

### 2. First-contact bootstrapping on chatmail

Chatmail servers require encryption for all messages. A fresh contact cannot send you a message until they have your public key. WKD is not auto-populated.

**Fix**: log the securejoin invite link at startup and share it with the first user:

```python
invite = rpc.get_chat_securejoin_qr_code(account_id, None)
print(f"Invite link: {invite}")
```

The user scans this QR code (or pastes the `OPENPGP4FPR:...` URL) in their Delta Chat app to establish an encrypted channel.

### 3. API version (`deltachat2>=0.9.0`)

Use the new API — the old `Rpc(accounts_dir=...)` constructor and `configure()` method are deprecated:

| Old (deprecated) | New |
|---|---|
| `Rpc(accounts_dir=path)` | `IOTransport(accounts_dir=path)` + `Rpc(transport)` |
| `rpc.configure(account_id, {...})` | `rpc.add_or_update_transport(account_id, {...})` |
| `rpc.get_chat_ids(account_id)` | `rpc.get_chatlist_entries(account_id, ...)` |

### 4. Fetching unread messages

For bot accounts use `get_next_msgs()`. Fall back to `get_fresh_msgs()` if unavailable:

```python
try:
    msg_ids = rpc.get_next_msgs(account_id)
except Exception:
    msg_ids = rpc.get_fresh_msgs(account_id)
```

### 5. State directory — create the parent, not the dir itself

The `accounts_dir` stores encryption keys and all message state. Deleting it means losing the bot's identity — all contacts must re-establish encrypted sessions via the invite link.

`deltachat-rpc-server` creates `accounts_dir` and `accounts.toml` itself on first run. Only create the **parent** directory in your code — if `accounts_dir` exists but has no `accounts.toml`, the binary errors with `"accounts.toml" does not exist`.

## Event types to handle

```python
EventType.INCOMING_MSG_BUNCH   # new messages arrived — call get_next_msgs()
EventType.INFO                 # informational log (safe to ignore)
EventType.WARNING              # warning from deltachat-core
EventType.MSGS_CHANGED         # message status changed
EventType.MSG_DELIVERED        # outbound message delivered
```

## Minimal async bot template

```python
import asyncio, tempfile
from pathlib import Path
from deltachat2 import IOTransport, Rpc, EventType, MsgData

async def run_bot(addr: str, password: str, accounts_dir: str = "~/.mybot/deltachat"):
    # Create the *parent* only — deltachat-rpc-server initialises accounts_dir
    # itself. Creating accounts_dir without accounts.toml causes an error.
    Path(accounts_dir).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def setup():
        transport = IOTransport(accounts_dir=str(Path(accounts_dir).expanduser()))
        transport.start()
        rpc = Rpc(transport)
        accounts = rpc.get_all_account_ids()
        account_id = accounts[0] if accounts else rpc.add_account()
        rpc.set_config(account_id, "bot", "1")
        if not rpc.is_configured(account_id):
            rpc.add_or_update_transport(account_id, {"addr": addr, "password": password})
        # Force keypair generation
        with tempfile.TemporaryDirectory() as tmp:
            rpc.export_self_keys(account_id, tmp, None)
        rpc.start_io(account_id)
        invite = rpc.get_chat_securejoin_qr_code(account_id, None)
        print(f"Invite: {invite}")
        return rpc, transport, account_id

    rpc, transport, account_id = await asyncio.to_thread(setup)

    try:
        while True:
            raw = await asyncio.to_thread(rpc.get_next_event)
            if raw.event.kind == EventType.INCOMING_MSG_BUNCH:
                msg_ids = await asyncio.to_thread(rpc.get_next_msgs, account_id)
                for msg_id in msg_ids:
                    msg = await asyncio.to_thread(rpc.get_message, account_id, msg_id)
                    if msg.text:
                        reply = MsgData(text=f"Echo: {msg.text}")
                        await asyncio.to_thread(rpc.send_msg, account_id, msg.chat_id, reply)
                if msg_ids:
                    await asyncio.to_thread(rpc.markseen_msgs, account_id, msg_ids)
    finally:
        await asyncio.to_thread(rpc.stop_io, account_id)
        transport.close()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--addr", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--accounts-dir", default="~/.mybot/deltachat")
    args = p.parse_args()
    asyncio.run(run_bot(args.addr, args.password, args.accounts_dir))
```
