# dgb-flowlab

A persistent, restart-safe engine for running **transaction-flow experiments between wallets you
control**, on **your own local DigiByte Core node**. Separate from CorePal on purpose (see
CorePal's ROADMAP: autonomous multi-hop engines stay out of its codebase, wallet and node
credentials).

Principle: *configure once, approve once, observe everything, execute autonomously, validate
continuously, record everything, reconcile everything, stop cleanly.*

## Status

| Phase | What | State |
|---|---|---|
| 1 | Flow / experiment state machine, one-time approval, audit log, write-ahead action journal, restart recovery, emergency stop | **done, 25 tests** |
| 2 | DigiByte Core RPC integration (the real `Verifier`, address generation, raw tx build/broadcast) | next |
| 3 | Planner, workload modes, seeded randomization, dynamic job generation, scheduling | |
| 4 | Execution loop, accounting and reconciliation | |
| 5+ | Transaction graph, timeline, analysis plugins, dashboard, export, replay | |

Phase 1 touches no node, holds no keys and broadcasts nothing. Python 3.10+, standard library only.

## Run the tests

    python -m unittest discover -s tests -v

## Properties Phase 1 guarantees (each has a test, and was mutation-checked)

* **One approval, pinned.** Approval must quote the SHA-256 of the exact configuration that was
  reviewed. Afterwards the config is immutable, in Python *and* by database trigger.
* **Fail closed.** Before any action the `Verifier` must positively report all of: wallet, balance,
  utxo, transaction, confirmation, height, flow_state. Missing, failing, raising or absent
  verifier => the experiment is PAUSED and nothing happens.
* **Write-ahead journal.** Intent is persisted before an action. A crash leaves it as `unknown`
  on restart; it is never retried automatically, and a unique index makes a second live action for
  the same job impossible. Interrupted broadcast cannot become a duplicate broadcast.
* **Unclean restart => RECOVERY**, and leaving it re-runs verification.
* **Emergency stop** aborts everything, blocks new actions, deletes nothing, and still lets
  already-broadcast transactions be recorded as they confirm.
* **Append-only audit log** (triggers), secrets redacted, amounts in integer satoshis.
* **Reconciliation failure => ERROR / REVIEW**, never COMPLETE.

## Security rules (from the spec, binding on later phases)

Localhost only. No cloud, no third-party transaction APIs. Never log or export RPC credentials,
private keys or seed phrases. Use dedicated wallets for experiments, never wallets that hold
other funds, and a dedicated RPC user whitelisted to only the methods the engine needs.
