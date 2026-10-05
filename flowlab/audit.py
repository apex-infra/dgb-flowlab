"""
audit.py -- append-only audit log with secret redaction.

Spec: never log private keys, RPC passwords or seed phrases. (The
experiment's *random seed* is an experimental parameter and IS logged;
"seed phrase" / mnemonic material is not.) Redaction is by key name
anywhere in the nested detail, plus a value check for key-shaped strings.
"""

import json
import re

_SECRET_KEY = re.compile(
    r"(rpc.?pass|rpc.?user|password|passphrase|priv(ate)?.?key|mnemonic|seed.?phrase|wif|xprv|secret)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"^(xprv|tprv|dprv)[0-9A-Za-z]{20,}")
REDACTED = "[REDACTED]"


def redact(obj):
    if isinstance(obj, dict):
        return {
            k: (REDACTED if _SECRET_KEY.search(str(k)) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and _SECRET_VALUE.match(obj):
        return REDACTED
    return obj


def has_secret_keys(obj):
    """True if any dict key anywhere in obj looks like a credential."""
    if isinstance(obj, dict):
        return any(_SECRET_KEY.search(str(k)) or has_secret_keys(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(has_secret_keys(v) for v in obj)
    return False


def write(conn, ts, event, *, experiment_id=None, flow_id=None, job_id=None,
          state=None, txid=None, wallet=None, address=None, amount_sats=None,
          resulting_state=None, detail=None):
    """Insert one audit row. Must be called inside the same tx as the change it records."""
    conn.execute(
        """INSERT INTO audit_log
           (ts, event, experiment_id, flow_id, job_id, state, txid, wallet,
            address, amount_sats, resulting_state, detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, event, experiment_id, flow_id, job_id, state, txid, wallet,
         address, amount_sats, resulting_state,
         json.dumps(redact(detail or {}), sort_keys=True)),
    )
