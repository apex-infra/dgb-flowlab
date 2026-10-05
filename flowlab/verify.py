"""
verify.py -- the pre-action verification interface.

Spec: before every action, independently verify wallet state, balance,
UTXO state, transaction state, confirmation state, blockchain height and
flow state; if anything is off, pause rather than continue.

Phase 1 defines the contract. Phase 2 supplies the real implementation
(DigiByte Core RPC). The engine treats this as untrusted input:
  * a check the verifier does not report counts as FAILED (fail closed)
  * a verifier that raises counts as FAILED
  * an unknown extra check is ignored, never treated as a pass
"""

from .states import REQUIRED_CHECKS


class Verifier:
    """Subclass and implement verify()."""

    def verify(self, experiment, flow, action):
        """
        experiment: dict row; flow: dict row or None; action: short string
        ('start', 'advance:PLAN', 'begin:broadcast', 'resume', ...).
        Return {check_name: (ok: bool, detail: str)} for REQUIRED_CHECKS.
        """
        raise NotImplementedError


def evaluate(verifier, experiment, flow, action):
    """Run the verifier and return a list of human-readable failures ([] == all clear)."""
    if verifier is None:
        return ["no verifier supplied"]
    try:
        results = verifier.verify(experiment, flow, action)
    except Exception as e:  # noqa: BLE001 - any verifier failure must fail closed
        return [f"verifier raised {type(e).__name__}: {e}"]
    if not isinstance(results, dict):
        return ["verifier returned a non-dict result"]
    failures = []
    for name in REQUIRED_CHECKS:
        if name not in results:
            failures.append(f"{name}: not reported (fail closed)")
            continue
        ok, detail = results[name]
        if ok is not True:
            failures.append(f"{name}: {detail}")
    return failures
