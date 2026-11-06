"""Native inbox delivery: a durable intent is never represented as a receipt."""
import hashlib
import json
import os
import subprocess
import uuid
from urllib.parse import quote, urlencode


def deliver(bridge, message, runner=subprocess.run):
    required = ("id", "recipient", "session_id", "operation_id", "body")
    if any(not isinstance(message.get(key), str) or not message[key] for key in required):
        raise ValueError("invalid_native_mail")
    if len(message["body"].encode()) > 12000 or type(message.get("epoch")) is not int or message["epoch"] < 1:
        raise ValueError("invalid_native_mail")
    db, journal = bridge.journal.db, bridge.journal
    db.execute("""CREATE TABLE IF NOT EXISTS native_mail(
      id TEXT NOT NULL,epoch INTEGER NOT NULL,digest TEXT NOT NULL,operation_id TEXT NOT NULL,
      state TEXT NOT NULL,receipt TEXT,PRIMARY KEY(id,epoch))""")
    digest = hashlib.sha256(json.dumps({key: message[key] for key in (*required, "epoch")}, sort_keys=True).encode()).hexdigest()
    saved = db.execute("SELECT * FROM native_mail WHERE id=? AND epoch=?", (message["id"], message["epoch"])).fetchone()
    path = "/api/messages/" + quote(message["id"], safe="") + "/delivered?" + urlencode({"mailbox": message["recipient"]})
    key = f'mail:{message["id"]}:{message["epoch"]}'
    if saved:
        if saved["digest"] != digest:
            raise ValueError("native_mail_conflict")
        if saved["state"] != "receipted":
            raise RuntimeError("native_mail_outcome_uncertain")
        journal.emit(key, path, json.loads(saved["receipt"]))
        return json.loads(saved["receipt"])["native_receipt"]
    session = db.execute("SELECT * FROM launches WHERE session_id=?", (message["session_id"],)).fetchone()
    if not session or session["state"] != "running" or not session["native_id"]:
        raise RuntimeError("native_mail_target_unavailable")
    native = bridge.lookup(session["native_id"], session)
    if not native or native.get("phase", native.get("state")) != "running":
        raise RuntimeError("native_mail_target_unavailable")
    claim = bridge.client.request("POST", path, {"phase": "claim", "epoch": message["epoch"], "operation_id": message["operation_id"]})
    if not isinstance(claim.get("claim_id"), str) or claim.get("epoch") != message["epoch"]:
        raise RuntimeError("native_mail_claim_unverified")
    with db:
        db.execute("INSERT INTO native_mail VALUES(?,?,?,?,'executing',NULL)",
                   (message["id"], message["epoch"], digest, message["operation_id"]))
    environment = os.environ.copy()
    for name in ("APLEXER_SESSION_ID", "APLEXER_TAG", "APLEXER_WORKSPACE", "APLEXER_PARENT_SESSION"):
        environment.pop(name, None)
    result = runner([bridge.executable, "message", "send", "--to", session["tag"], "--kind", "hub2",
                     "--data", json.dumps({"message_id": message["id"], "epoch": message["epoch"], "recipient": message["recipient"]}),
                     message["body"]], cwd=session["workspace"], env=environment,
                    capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("native_mail_effect_unconfirmed")
    try:
        receipt_id = str(uuid.UUID(result.stdout.strip()))
    except ValueError:
        raise RuntimeError("native_mail_receipt_invalid") from None
    receipt = {"provider": "aplexer", "message_id": receipt_id,
               "session_id": message["session_id"], "recipient": message["recipient"]}
    payload = {"phase": "receipt", "epoch": message["epoch"], "operation_id": message["operation_id"],
               "claim_id": claim["claim_id"], "native_receipt": receipt}
    with db:
        db.execute("UPDATE native_mail SET state='receipted',receipt=? WHERE id=? AND epoch=?",
                   (json.dumps(payload), message["id"], message["epoch"]))
        db.execute("INSERT OR REPLACE INTO outbox VALUES(?,?,?)", (key, path, json.dumps(payload)))
    journal.flush(bridge.client)
    return receipt
