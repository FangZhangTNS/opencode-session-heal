# opencode-session-heal

OpenCode V2 plugin that repairs sessions blocked by caller-bound reasoning items.

## The problem

opencode replays reasoning parts that carry `metadata.openai` as encrypted reasoning items. Those items are bound to the caller that created them, so after an API key change the upstream rejects the replay:

```
reasoning `encrypted_content` was not issued to this caller
```

The session is stuck: every new turn re-sends the same history and hits the same rejection. The reasoning text itself is fine; only the encrypted continuation is unusable.

## What it does

Detects that specific rejection, marks the session, and repairs it once on the next start by dropping only the encrypted continuation fields:

| Storage | Field removed |
| --- | --- |
| legacy `part` table | `data.metadata.openai` |
| v2 `session_message` table | `data.content[].state.{itemId, reasoningEncryptedContent}` |

Reasoning text stays. No row is deleted, no other column is touched, and the operation is idempotent.

A row-level undo file is written before the update, so every heal can be reverted.

## Half-automatic on purpose

Swapping an API key is a human decision, and a plugin should not fabricate one. The flow is:

1. The upstream returns the rejection. The plugin marks the session `pending`, logs the session id, and does nothing else.
2. You switch the API key and restart opencode.
3. On startup the plugin heals pending sessions once and reports how many rows changed.
4. A healed session is never processed again. New encrypted items keep working normally.

## Install

```bash
git clone https://github.com/FangZhangTNS/opencode-session-heal
```

Point `opencode.json` at the checkout:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/opencode-session-heal"]
}
```

Or copy the directory into the global plugin folder and use a relative path:

```json
{
  "plugins": ["./plugins/opencode-session-heal"]
}
```

Requirements:

- OpenCode V2 (developed and tested against 2.0.29)
- Python 3 on `PATH`, or `SESSION_HEAL_PYTHON` pointing at an interpreter

The plugin probes `SESSION_HEAL_PYTHON`, then `python3`, then `python`, then `py -3`. With no interpreter it logs a warning and leaves sessions pending instead of blocking startup.

## State

State lives in plugin storage (`ctx.storage`), not in files. Removing the plugin removes its state with it.

Diagnostics go to opencode's own log through `console.info` / `console.error`.

## Undo

Each heal writes `<db dir>/session-heal-undo/<session>_<timestamp>.json` with the exact rows it is about to change.

```bash
python scripts/session-heal-fix.py --restore <undo.json>
```

Restoring brings the encrypted fields back, which also brings the original rejection back; use it only to inspect or to hand a session to a different caller.

## Limits

- It edits opencode's sqlite schema directly (`part`, `session_message`). A schema change in a future OpenCode release can break it; the plugin reports a failure and changes nothing when a table or column is missing.
- It does not swap keys, retry the failed turn, or notify a human. It only clears the unusable history so the next attempt can succeed.
- The undo file is a row snapshot, not a database backup.

## Development

```bash
python tests/test_fix.py          # builds a throwaway db, heals, checks, restores
node_modules/.bin/tsc -p .        # type check
```

## License

MIT
