#!/usr/bin/env python3
"""One-shot heal for opencode sessions blocked by caller-bound reasoning items.

Background: opencode replays reasoning parts that carry metadata.openai as
encrypted reasoning items. Those items are bound to the caller that created
them, so after an API key change the upstream rejects the replay with:

    reasoning `encrypted_content` was not issued to this caller

This script removes only the encrypted continuation state from reasoning parts
created before the cut point. The reasoning text itself is kept, and the part
stops being turned into an encrypted item, so the session opens again.

Two storage layouts are handled (both can exist side by side since v2):

  A) legacy part table
     data.type='reasoning' and data.metadata.openai present
     -> json_remove(data,'$.metadata.openai')

  B) v2 native session_message table
     data.content[].state.{itemId,reasoningEncryptedContent}
     -> drop the state fields

Only those fields are touched: no row is deleted, no text is changed, and the
operation is idempotent.

The database can be tens of gigabytes, so there is no full backup. Instead the
rows about to change are saved verbatim into a small undo file, which
--restore can replay.

Usage:
  python session-heal-fix.py --session ses_x --cut-at 1789335901000
  python session-heal-fix.py --session ses_x --cut-at 1789335901000 --dry-run
  python session-heal-fix.py --restore <undo.json>

Exit code: 0 on success, 1 on failure.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys

DB_WHERE = (
    "session_id = ? AND json_extract(data,'$.type') = 'reasoning' "
    "AND json_extract(data,'$.metadata.openai') IS NOT NULL AND time_created < ?"
)

V2_TABLE = "session_message"
V2_WHERE = (
    f"session_id = ? AND time_created < ? AND data LIKE '%reasoningEncryptedContent%'"
)


def default_db() -> str:
    override = os.environ.get("OPENCODE_DB")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")


def strip_v2_data(text):
    """Drop the encrypted continuation state from a v2 reasoning entry.

    Both shapes are covered:
      content[].state.{itemId, reasoningEncryptedContent}
      content[].state.openai.{itemId, reasoningEncryptedContent}

    Returns (new_data, removed_count); (None, 0) when nothing needs changing.
    """
    try:
        obj = json.loads(text)
    except Exception:
        return None, 0
    content = obj.get("content")
    if not isinstance(content, list):
        return None, 0
    removed = 0
    for entry in content:
        if not isinstance(entry, dict) or entry.get("type") != "reasoning":
            continue
        state = entry.get("state")
        if not isinstance(state, dict):
            continue
        target = state.get("openai") if isinstance(state.get("openai"), dict) else state
        hit = False
        for key in ("itemId", "reasoningEncryptedContent"):
            if key in target:
                target.pop(key, None)
                hit = True
        if not hit:
            continue
        if isinstance(state.get("openai"), dict) and not state["openai"]:
            state.pop("openai", None)
        if not state:
            entry.pop("state", None)
        removed += 1
    if removed == 0:
        return None, 0
    return json.dumps(obj, ensure_ascii=False), removed


def emit(line: str) -> None:
    print(line, flush=True)


def write_undo(undo_dir, db_path, session, cut, rows, v2_rows=()):
    os.makedirs(undo_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(undo_dir, f"{session}_{stamp}.json")
    entries = [{"table": "part", "id": r[0], "data": r[1]} for r in rows]
    entries += [{"table": V2_TABLE, "id": r[0], "data": r[1]} for r in v2_rows]
    payload = {
        "session": session,
        "cutAt": cut,
        "createdAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "sourceDb": db_path,
        "rows": entries,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path, os.path.getsize(path)


def do_restore(path, db_override):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("rows") or []
    db = db_override or payload.get("sourceDb") or default_db()
    con = sqlite3.connect(db, timeout=15)
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        restored = 0
        for row in rows:
            table = row.get("table") or "part"
            con.execute(f"UPDATE {table} SET data = ? WHERE id = ?", (row["data"], row["id"]))
            restored += 1
        con.commit()
        emit(f"RESTORE ok rows={restored} db={db} from={path}")
        return 0
    except Exception as exc:
        emit(f"RESTORE FAIL err={exc}")
        return 1
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="session id")
    ap.add_argument("--cut-at", type=int, help="cut point, epoch milliseconds")
    ap.add_argument("--db", help="path to opencode.db (default: ~/.local/share/opencode/opencode.db)")
    ap.add_argument("--undo-dir", help="directory for undo files (default: <db dir>/session-heal-undo)")
    ap.add_argument("--dry-run", action="store_true", help="count only, change nothing")
    ap.add_argument("--undo-only", action="store_true", help="write the undo file, change nothing")
    ap.add_argument("--restore", help="replay an undo file")
    args = ap.parse_args()

    if args.restore:
        if not os.path.exists(args.restore):
            emit(f"RESTORE FAIL reason=file-missing path={args.restore}")
            return 1
        return do_restore(args.restore, args.db)

    if not args.session or args.cut_at is None:
        emit("FAIL reason=missing-args need=--session --cut-at")
        return 1

    db = args.db or default_db()
    session, cut = args.session, args.cut_at
    undo_dir = args.undo_dir or os.path.join(os.path.dirname(os.path.abspath(db)), "session-heal-undo")
    if not os.path.exists(db):
        emit(f"FAIL session={session} reason=db-missing path={db}")
        return 1

    try:
        con = sqlite3.connect(db, timeout=15)
        con.execute("PRAGMA busy_timeout = 15000")
    except Exception as exc:
        emit(f"FAIL session={session} reason=open-failed err={exc}")
        return 1

    try:
        # A) legacy part table
        target = con.execute(f"SELECT COUNT(*) FROM part WHERE {DB_WHERE}", (session, cut)).fetchone()[0]
        keep_text = con.execute(
            "SELECT COUNT(*) FROM part WHERE session_id = ? "
            "AND json_extract(data,'$.type') = 'reasoning' "
            "AND length(json_extract(data,'$.text')) > 0",
            (session,),
        ).fetchone()[0]

        # B) v2 native session_message table
        v2_plan = []
        v2_target = 0
        for rid, rdata in con.execute(
            f"SELECT id, data FROM {V2_TABLE} WHERE {V2_WHERE}", (session, cut)
        ).fetchall():
            new_data, count = strip_v2_data(rdata)
            if count:
                v2_plan.append((rid, rdata, new_data, count))
                v2_target += count

        if target == 0 and v2_target == 0:
            emit(
                f"OK session={session} target=0 v2_target=0 removed=0 "
                f"keep_text={keep_text} note=already-clean"
            )
            return 0

        if args.dry_run:
            emit(
                f"DRYRUN session={session} target={target} v2_target={v2_target} "
                f"v2_rows={len(v2_plan)} removed=0 keep_text={keep_text}"
            )
            return 0

        rows = con.execute(f"SELECT id, data FROM part WHERE {DB_WHERE}", (session, cut)).fetchall()
        undo_path, undo_size = write_undo(
            undo_dir, db, session, cut, rows, [(r[0], r[1]) for r in v2_plan]
        )

        if args.undo_only:
            emit(
                f"UNDO session={session} target={target} v2_target={v2_target} "
                f"rows={len(rows)} v2_rows={len(v2_plan)} undo={undo_path} size={undo_size}"
            )
            return 0

        cursor = con.execute(
            f"UPDATE part SET data = json_remove(data,'$.metadata.openai') WHERE {DB_WHERE}",
            (session, cut),
        )
        removed = cursor.rowcount

        v2_removed = 0
        for rid, _old, new_data, count in v2_plan:
            con.execute(f"UPDATE {V2_TABLE} SET data = ? WHERE id = ?", (new_data, rid))
            v2_removed += count
        con.commit()

        left = con.execute(f"SELECT COUNT(*) FROM part WHERE {DB_WHERE}", (session, cut)).fetchone()[0]
        left_v2 = con.execute(
            f"SELECT COUNT(*) FROM {V2_TABLE} WHERE {V2_WHERE}", (session, cut)
        ).fetchone()[0]
        verify = "yes" if left == 0 and left_v2 == 0 else "no"
        emit(
            f"OK session={session} target={target} removed={removed} left={left} "
            f"v2_target={v2_target} v2_removed={v2_removed} left_v2={left_v2} "
            f"keep_text={keep_text} verify={verify} undo={undo_path} size={undo_size}"
        )
        return 0
    except Exception as exc:
        emit(f"FAIL session={session} reason=update-failed err={exc}")
        return 1
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
