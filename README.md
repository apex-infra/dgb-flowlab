# dgb-flowlab

A persistent, restart-safe engine for running **transaction-flow experiments between wallets you
control**, on **your own local DigiByte Core node**. Separate from CorePal on purpose: autonomous
multi-hop engines stay out of its codebase, wallet and node credentials.

Principle: *configure once, approve once, observe everything, execute autonomously, validate
continuously, record everything, reconcile everything, stop cleanly.*

## Status

| Phase | What | State |
|---|---|---|
| 1 | Experiment/flow state machine, one-time approval, audit log, write-ahead journal, restart recovery, emergency stop | done |
| 2 | DigiByte Core RPC client (method-whitelisted), node verifier, transaction builder | done |
| 3 | Planner: explicit, deterministic hops; `repeat` shorthand; sweep hops (`"all"`) | done |
| 4 | Executor, accounting and reconciliation; command line (`flowctl.py`) | done, three real mainnet runs |
| 5 | Terminal dashboard (`watch`) and local web dashboard (`serve`) with operator controls | done |
| 6 | Export, replay, analysis | next |

Randomization is not supported and is not planned: the planner refuses it. Every hop, amount and
delay is written down before approval and cannot change afterwards.

## Using it

    python3 flowctl.py serve             # the dashboard, http://127.0.0.1:8787, full control
    python3 flowctl.py serve --read-only # same page with every action switched off
    python3 flowctl.py serve --port 9000
    python3 flowctl.py watch             # live terminal view, read-only

Everything below can be done from the web page (New experiment, approve, start, pause, emergency
stop, clear stop, resume, recover, resolve). The command line does the same:

    python3 flowctl.py new CONFIG.json    create and review
    python3 flowctl.py approve EXP HASH   approve once, quoting the hash from the review
    python3 flowctl.py run EXP            run to the end (Ctrl+C is safe)
    python3 flowctl.py status EXP | list | resume EXP | recover EXP | resolve ... | stop

Local settings (RPC user, port, wallet names, fee cap) live in `config.local.json`, which is
git-ignored. The first hop sends a fixed amount; later hops can send their wallet's whole balance
minus the fee (`"amount_sats": "all"`), which leaves no change outputs behind. `"all"` is refused
out of the source wallet. The dashboard additionally refuses any plan whose last hop does not end
in the destination wallet.

## Dashboard safety

* Binds to 127.0.0.1 only, with no option to change that. Reach it from another computer through
  an SSH tunnel, never by opening the port.
* Every request must carry a Host header for this machine and port (DNS-rebinding guard).
* Every action is a POST that needs the per-run secret token, a same-origin Origin header and a
  JSON body. The page loads only its own files (strict Content-Security-Policy).
* Approval needs the first 8 characters of the config hash typed in, is one-time, and is pinned to
  the exact config. The actions are the engine's own, so verification and the journal still apply.
* One run at a time. Closing the server halts the run between steps and records a clean shutdown.

## Properties the engine guarantees (each has a test, and was mutation-checked)

* **One approval, pinned.** Approval must quote the SHA-256 of the exact configuration reviewed.
  Afterwards the config is immutable, in Python and by database trigger.
* **Fail closed.** Before any action the verifier must positively report wallet, balance, utxo,
  transaction, confirmation, height and flow state. Anything missing or failing pauses the run.
* **Write-ahead journal.** Intent is persisted before an action. A crash leaves it `unknown` on
  restart and it is never retried automatically, so an interrupted broadcast cannot be duplicated.
* **Unclean restart goes to RECOVERY**, and leaving it re-runs verification.
* **Emergency stop** aborts everything, blocks new actions, deletes nothing.
* **Append-only audit log**, secrets redacted, amounts in integer satoshis.
* **Reconciliation failure goes to ERROR / REVIEW**, never COMPLETE.

## Tests

    python3 -m unittest discover -s tests -t .     # 132 tests; about 3 to 4 minutes on the node box

## Security rules

Localhost only. No cloud, no third-party transaction APIs. Never log or export RPC credentials,
private keys or seed phrases. Use dedicated wallets for experiments, never wallets that hold other
funds, and a dedicated RPC user whitelisted to only the methods the engine needs (28 today).
