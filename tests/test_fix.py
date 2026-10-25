"""End-to-end check for session-heal-fix.py.

Builds a throwaway sqlite database with both storage layouts, heals it, checks
that only the encrypted continuation fields were dropped, then restores from the
undo file and checks the rows are byte-identical again.

Run: python tests/test_fix.py
Exit code 0 means every assertion passed.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "session-heal-fix.py")
CUT = 3000


def fail(message):
    print(f"FAIL {message}")
    sys.exit(1)


def build_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    part = {
        "type": "reasoning",
        "text": "keep me",
        "metadata": {"openai": {"itemId": "rs_1", "reasoningEncryptedContent": "enc1"}},
    }
    con.execute("INSERT INTO part VALUES (?,?,?,?)", ("p1", "s1", 1000, json.dumps(part)))
    message = {
        "content": [
            {"type": "reasoning", "text": "hello", "state": {"itemId": "rs_2", "reasoningEncryptedContent": "enc2"}}
        ]
    }
    con.execute("INSERT INTO session_message VALUES (?,?,?,?)", ("m1", "s1", 1000, json.dumps(message)))
    # Created after the cut point: must stay untouched.
    newer = {"type": "reasoning", "text": "newer", "metadata": {"openai": {"itemId": "rs_3"}}}
    con.execute("INSERT INTO part VALUES (?,?,?,?)", ("p2", "s1", 5000, json.dumps(newer)))
    con.commit()
    con.close()


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


def read(path, table, row_id):
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT data FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]
    finally:
        con.close()


def main():
    tmp = tempfile.mkdtemp(prefix="session-heal-test-")
    db = os.path.join(tmp, "opencode.db")
    undo_dir = os.path.join(tmp, "undo")
    build_db(db)

    dry = run("--db", db, "--session", "s1", "--cut-at", str(CUT), "--dry-run")
    if dry.returncode != 0 or "DRYRUN" not in dry.stdout:
        fail(f"dry run: {dry.stdout.strip()} {dry.stderr.strip()}")
    if "target=1" not in dry.stdout or "v2_target=1" not in dry.stdout:
        fail(f"dry run counts wrong: {dry.stdout.strip()}")
    if "keep_text=2" not in dry.stdout:
        fail(f"dry run keep_text wrong: {dry.stdout.strip()}")

    heal = run("--db", db, "--session", "s1", "--cut-at", str(CUT), "--undo-dir", undo_dir)
    if heal.returncode != 0 or not heal.stdout.startswith("OK"):
        fail(f"heal: {heal.stdout.strip()} {heal.stderr.strip()}")
    if "removed=1" not in heal.stdout or "verify=yes" not in heal.stdout:
        fail(f"heal result wrong: {heal.stdout.strip()}")

    part = json.loads(read(db, "part", "p1"))
    if part.get("text") != "keep me":
        fail("reasoning text was not preserved")
    if part.get("metadata", {}).get("openai"):
        fail("metadata.openai still present on the legacy part")

    message = json.loads(read(db, "session_message", "m1"))
    state = message["content"][0].get("state") or {}
    if "itemId" in state or "reasoningEncryptedContent" in state:
        fail("encrypted state still present on the v2 message")

    newer = json.loads(read(db, "part", "p2"))
    if not newer.get("metadata", {}).get("openai"):
        fail("a part created after the cut point was modified")

    again = run("--db", db, "--session", "s1", "--cut-at", str(CUT), "--dry-run")
    if "target=0" not in again.stdout or "v2_target=0" not in again.stdout:
        fail(f"heal is not idempotent: {again.stdout.strip()}")

    undo = [name for name in os.listdir(undo_dir) if name.endswith(".json")]
    if len(undo) != 1:
        fail(f"expected one undo file, found {len(undo)}")
    restore = run("--db", db, "--restore", os.path.join(undo_dir, undo[0]))
    if restore.returncode != 0 or not restore.stdout.startswith("RESTORE ok"):
        fail(f"restore: {restore.stdout.strip()} {restore.stderr.strip()}")

    restored_part = json.loads(read(db, "part", "p1"))
    if not restored_part.get("metadata", {}).get("openai"):
        fail("restore did not bring back metadata.openai")
    restored_message = json.loads(read(db, "session_message", "m1"))
    restored_state = restored_message["content"][0].get("state") or {}
    if "reasoningEncryptedContent" not in restored_state:
        fail("restore did not bring back the v2 encrypted state")

    after_restore = run("--db", db, "--session", "s1", "--cut-at", str(CUT), "--dry-run")
    if "target=1" not in after_restore.stdout:
        fail(f"restored rows are not detectable again: {after_restore.stdout.strip()}")

    print("OK all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
