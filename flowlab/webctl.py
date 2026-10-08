"""
webctl.py -- every operator action the dashboard offers, as plain methods.

These are the same engine calls the command line makes (new, approve, run, stop,
resume, recover, resolve), with the same checks: one-time approval pinned to the
config hash, fail-closed verification, write-ahead journal. Nothing here can
bypass them. At most one run is active at a time, in a background thread.
"""

import base64
import subprocess
import threading
import time
from collections import deque

from .cli import _open, _run
from .config_schema import (
    destination_addresses,
    destination_endpoint,
    destination_is_external,
    destination_wallets,
    has_multi_destinations,
    validate,
)
from .plays import compile_play, get_play_spec

FINISHED = ("COMPLETE", "IDLE", "ABORTED")
FUND_ADDRESS_TYPES = frozenset({
    "legacy",
    "p2sh-segwit",
    "bech32",
    "bech32m",
})

# The dashboard accepts only the config fields it knows how to present and review.
TOP_KEYS = {
    "play",
    "flows",
    "workload",
    "confirmations_required",
    "fee_policy",
    "address_policy",
    "utxo_policy",
    "randomization",
    "finalization",
}
FLOW_KEYS = {
    "description",
    "source_wallet",
    "flow_wallets",
    "destination_wallet",
    "destination_address",
    "destinations",
    "allocation_wallet",
    "finalization_wallet",
    "allocation_sats",
    "repeat",
    "transfers",
    "experimental_topology",
}


class ControlError(Exception):
    pass


class _Halt(Exception):
    pass


def _text(body, key, required=True, limit=500):
    v = body.get(key, "")
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
        raise ControlError(f"'{key}' is required" if required else f"'{key}' is invalid")
    return v.strip()


class Controller:
    def __init__(self, db, rpc=None, builder=None, wallets=(), sleep=None, control=True,
                 wallet_roles=None):
        self.db, self.rpc, self.builder = db, rpc, builder
        self.wallets = list(wallets)
        self.wallet_roles = {
            k: list(v)
            for k, v in (wallet_roles or {}).items()
        }
        self.control, self._sleep = control, sleep
        self._lock, self._halt = threading.Lock(), threading.Event()
        self._thread, self.exp = None, None
        self.log = deque(maxlen=300)
        self.actions = {
            "review": self.review,
            "new": self.new,
            "compile_play": self.compile_play,
            "fund_reserve_address": self.fund_reserve_address,
            "fund_reserve_label": self.fund_reserve_label,
            "fund_reserve_qr": self.fund_reserve_qr,
            "approve": self.approve,
            "run": self.run,
            "stop": self.stop,
            "halt": self.halt,
            "clear_stop": self.clear_stop,
            "resume": self.resume,
            "recover": self.recover,
            "resolve": self.resolve,
        }

    # ----------------------------------------------------------------- plumbing
    def say(self, line):
        self.log.append({"t": time.strftime("%H:%M:%S"), "line": str(line)})

    def active(self):
        return bool(self._thread and self._thread.is_alive())

    def _with_engine(self, fn, node=False):
        if node and not (self.rpc and self.builder):
            raise ControlError("the node is not connected (check config.local.json)")
        e = _open(self.db, self.rpc if node else None)[0]
        try:
            return fn(e)
        finally:
            e.close(clean=False)

    def _wait(self, seconds):
        if self._halt.is_set():
            raise _Halt()
        if self._sleep:
            self._sleep(seconds)
        elif self._halt.wait(seconds):
            raise _Halt()

    def _not_while_running(self):
        if self.active():
            raise ControlError("a run is active; stop it first")

    def _validate_external_destinations(self, cfg):
        external = []

        for fl in cfg.get("flows", []):
            external.extend(destination_addresses(fl))

        if not external:
            return

        if not self.rpc:
            raise ControlError(
                "the node is not connected; external destinations "
                "cannot be validated"
            )

        for address in external:
            try:
                result = self.rpc.validate_address(address)
            except Exception as exc:
                raise ControlError(
                    f"could not validate external destination {address}: {exc}"
                ) from exc

            if not isinstance(result, dict) or result.get("isvalid") is not True:
                raise ControlError(
                    f"external destination is not a valid DigiByte address: "
                    f"{address}"
                )

    # ------------------------------------------------------------------ actions
    def review(self, body):
        exp = _text(body, "exp")
        return self._with_engine(lambda e: {"exp": exp, "text": e.review(exp)["text"],
                                            "hash": e.get_experiment(exp)["config_hash"]})

    def _reserve_wallet(self, body):
        wallet = _text(body, "wallet", limit=64)
        reserve = set(self.wallet_roles.get("reserve", []))

        if not reserve:
            raise ControlError("no reserve-role wallet is configured")

        if wallet not in reserve:
            raise ControlError(
                f"wallet {wallet} does not have the reserve role"
            )

        if wallet not in self.wallets:
            raise ControlError(
                f"reserve wallet {wallet} is outside the dashboard allowlist"
            )

        if not self.rpc:
            raise ControlError(
                "the node is not connected; reserve wallet action unavailable"
            )

        return wallet

    def fund_reserve_address(self, body):
        wallet = self._reserve_wallet(body)
        label = _text(
            body,
            "label",
            required=False,
            limit=100,
        )

        address_type = body.get("address_type", "bech32")
        if (
            not isinstance(address_type, str)
            or address_type not in FUND_ADDRESS_TYPES
        ):
            raise ControlError(
                "address_type must be one of: "
                "legacy, p2sh-segwit, bech32, bech32m"
            )

        try:
            address = self.rpc.get_new_address(
                wallet,
                label,
                address_type,
            )
        except Exception as exc:
            raise ControlError(
                f"could not generate reserve receiving address: {exc}"
            ) from exc

        if not isinstance(address, str) or not address:
            raise ControlError(
                "node returned an invalid reserve receiving address"
            )

        try:
            info = self.rpc.get_address_info(wallet, address)
        except Exception as exc:
            raise ControlError(
                f"could not verify reserve receiving address: {exc}"
            ) from exc

        if not isinstance(info, dict) or info.get("ismine") is not True:
            raise ControlError(
                "generated reserve receiving address is not owned by "
                f"{wallet}"
            )

        return {
            "wallet": wallet,
            "address": address,
            "label": label,
            "address_type": address_type,
        }

    def fund_reserve_label(self, body):
        wallet = self._reserve_wallet(body)
        address = _text(body, "address", limit=128)
        label = _text(
            body,
            "label",
            required=False,
            limit=100,
        )

        try:
            info = self.rpc.get_address_info(wallet, address)
        except Exception as exc:
            raise ControlError(
                f"could not verify reserve receiving address: {exc}"
            ) from exc

        if not isinstance(info, dict) or info.get("ismine") is not True:
            raise ControlError(
                "reserve receiving address is not owned by "
                f"{wallet}"
            )

        try:
            self.rpc.set_label(wallet, address, label)
        except Exception as exc:
            raise ControlError(
                f"could not update reserve address label: {exc}"
            ) from exc

        return {
            "wallet": wallet,
            "address": address,
            "label": label,
        }

    def fund_reserve_qr(self, body):
        uri = _text(body, "uri", limit=1000)

        if not uri.startswith("digibyte:"):
            raise ControlError(
                "payment URI must use the digibyte: scheme"
            )

        if any(ord(ch) < 32 or ord(ch) == 127 for ch in uri):
            raise ControlError(
                "payment URI contains invalid control characters"
            )

        try:
            result = subprocess.run(
                [
                    "/usr/bin/qrencode",
                    "-o", "-",
                    "-t", "PNG",
                    "-s", "6",
                    "-m", "3",
                ],
                input=uri.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError(
                f"could not generate payment QR: {type(exc).__name__}"
            ) from exc

        if result.returncode != 0:
            raise ControlError(
                "could not generate payment QR"
            )

        png = result.stdout

        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ControlError(
                "QR encoder returned invalid PNG data"
            )

        encoded = base64.b64encode(png).decode("ascii")

        return {
            "uri": uri,
            "image": "data:image/png;base64," + encoded,
        }

    def compile_play(self, body):
        name = _text(body, "play", limit=64)
        params = body.get("params")
        if not isinstance(params, dict):
            raise ControlError("'params' must be an object")

        cfg = compile_play(name, params)
        self._validate_external_destinations(cfg)

        # A dashboard play may only reference wallets configured for this
        # FlowLab instance. The play compiler itself stays environment-agnostic.
        used = set()
        for fl in cfg["flows"]:
            used.add(fl["source_wallet"])
            used.update(fl["flow_wallets"])
            used.update(destination_wallets(fl))

        outside = sorted(used - set(self.wallets))
        if outside:
            raise ControlError(
                "play references wallet(s) outside the dashboard allowlist: "
                + ", ".join(outside)
            )

        # When wallet-role metadata is configured, it is authoritative for
        # dashboard Plays. The generic play compiler intentionally remains
        # environment-agnostic.
        if self.wallet_roles:
            reserve = set(self.wallet_roles.get("reserve", []))
            stage_role = set(self.wallet_roles.get("stage", []))
            worker_role = set(self.wallet_roles.get("workers", []))
            hub_role = set(self.wallet_roles.get("hubs", []))
            workload = worker_role | hub_role
            destinations = set(self.wallet_roles.get("destinations", []))

            if name == "hub_and_spoke":
                hub = params.get("hub_wallet")
                if hub not in hub_role:
                    raise ControlError(
                        f"play hub wallet {hub} must have hub role"
                    )

                wrong_spokes = sorted(
                    set(params.get("workers", [])) - worker_role
                )
                if wrong_spokes:
                    raise ControlError(
                        "Hub-and-Spoke spoke wallet(s) must have worker role: "
                        + ", ".join(wrong_spokes)
                    )

            for fl in cfg["flows"]:
                source = fl["source_wallet"]
                stage = fl.get("allocation_wallet")

                if source not in reserve:
                    raise ControlError(
                        f"play source wallet {source} must have reserve role"
                    )

                if stage not in stage_role:
                    raise ControlError(
                        f"play allocation wallet {stage} must have stage role"
                    )

                for destination in destination_wallets(fl):
                    if destination not in destinations:
                        raise ControlError(
                            f"play destination wallet {destination} "
                            "must have destination role"
                        )

                workers = set(fl["flow_wallets"])
                if stage is not None:
                    workers.discard(stage)

                wrong_workers = sorted(workers - workload)
                if wrong_workers:
                    raise ControlError(
                        "play workload wallet(s) must have worker or hub role: "
                        + ", ".join(wrong_workers)
                    )

        return {
            "play": get_play_spec(name),
            "config": cfg,
        }

    def new(self, body):
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise ControlError("'config' must be an object")
        extra = set(cfg) - TOP_KEYS
        flows = cfg.get("flows") if isinstance(cfg.get("flows"), list) else []
        for fl in flows:
            extra |= set(fl) - FLOW_KEYS if isinstance(fl, dict) else set()
        if extra:
            raise ControlError("not supported here: " + ", ".join(sorted(extra)))
        cfg = validate(cfg)
        self._validate_external_destinations(cfg)

        experimental = bool(cfg.get("randomization", {}).get("enabled"))
        if not experimental:
            if any(has_multi_destinations(fl) for fl in cfg["flows"]):
                raise ControlError(
                    "multi-destination deterministic execution is not "
                    "supported yet"
                )

            reserve = set(self.wallet_roles.get("reserve", []))
            stage_role = set(self.wallet_roles.get("stage", []))
            workload = (
                set(self.wallet_roles.get("workers", []))
                | set(self.wallet_roles.get("hubs", []))
            )
            destinations = set(self.wallet_roles.get("destinations", []))

            for fl in cfg["flows"]:
                ts = fl.get("transfers") or []
                endpoint = destination_endpoint(fl)

                if not ts or ts[-1]["to"] != endpoint:
                    raise ControlError(
                        "the last hop must end in the configured destination"
                    )

                if self.wallet_roles:
                    source = fl["source_wallet"]

                    if source not in reserve:
                        raise ControlError(
                            f"deterministic source wallet {source} "
                            "must have reserve role"
                        )

                    if not destination_is_external(fl):
                        destination = fl["destination_wallet"]
                        if destination not in destinations:
                            raise ControlError(
                                f"deterministic destination wallet {destination} "
                                "must have destination role"
                            )

                    intermediates = set(fl["flow_wallets"])
                    allocation_wallet = fl.get("allocation_wallet")

                    if allocation_wallet is not None:
                        if allocation_wallet not in stage_role:
                            raise ControlError(
                                f"deterministic allocation wallet "
                                f"{allocation_wallet} must have stage role"
                            )
                        intermediates.discard(allocation_wallet)

                    wrong_workers = sorted(
                        intermediates - workload
                    )
                    if wrong_workers:
                        raise ControlError(
                            "deterministic intermediate wallet(s) must have "
                            "worker or hub role: "
                            + ", ".join(wrong_workers)
                        )

        def go(e):
            exp = e.create_experiment(_text(body, "description", required=False, limit=200))
            h = e.configure_experiment(exp, cfg)
            return {"exp": exp, "hash": h, "text": e.review(exp)["text"]}
        return self._with_engine(go)

    def approve(self, body):
        exp, h = _text(body, "exp"), _text(body, "hash", limit=64)
        self._with_engine(lambda e: e.approve(exp, h, _text(body, "note", required=False, limit=200)))
        return {"ok": True}

    def run(self, body):
        exp = _text(body, "exp")
        state = self._with_engine(lambda e: e.get_experiment(exp)["state"])
        if state in ("CREATED", "CONFIGURED"):
            raise ControlError("approve it first")
        if state in FINISHED:
            raise ControlError(f"the experiment is {state}")
        if not (self.rpc and self.builder):
            raise ControlError("the node is not connected (check config.local.json)")
        with self._lock:
            self._not_while_running()
            self._halt.clear()
            self.exp = exp
            self._thread = threading.Thread(target=self._runner, args=(exp,), daemon=True)
            self._thread.start()
        return {"ok": True}

    def _runner(self, exp):
        e, clean = None, False
        try:
            e = _open(self.db, self.rpc)[0]
            code = _run(e, self.rpc, self.builder, exp, self.say, self._wait)
            self.say(f"run ended (code {code})")
            clean = True
        except _Halt:
            self.say("stopped. Press Continue to carry on.")
            clean = True
        except Exception as err:  # noqa: BLE001
            self.say(f"ERROR: {type(err).__name__}: {err}")
        finally:
            if e is not None:
                e.close(clean=clean)

    def stop(self, body):
        why = "operator emergency stop (dashboard)"
        stopped = self._with_engine(lambda e: e.emergency_stop(why))
        self._halt.set()
        self.say(f"EMERGENCY STOP: aborted {stopped}")
        return {"aborted": stopped}

    def halt(self, body):
        """Stop the run between steps without aborting the experiment; Continue picks it up again."""
        self._halt.set()
        self.say("halting after the current step")
        return {"ok": True}

    def clear_stop(self, body):
        note = _text(body, "note")
        self._with_engine(lambda e: e.clear_emergency_stop(note))
        self.say("emergency stop cleared")
        return {"ok": True}

    def resume(self, body):
        self._not_while_running()
        exp = _text(body, "exp")

        def go(e):
            e.recover()
            e.resume(exp)
        self._with_engine(go, node=True)
        return {"ok": True}

    def recover(self, body):
        self._not_while_running()
        exp = _text(body, "exp")

        def go(e):
            s = e.recover()
            left = e.get_experiment(exp)["state"] == "RECOVERY"
            if left:
                e.leave_recovery(exp)
            return {"clean_shutdown": s["clean_shutdown"], "interrupted": s["unknown_actions"], "left_recovery": left}
        return self._with_engine(go, node=True)

    def resolve(self, body):
        action, outcome = body.get("action"), body.get("outcome")
        if not isinstance(action, int) or isinstance(action, bool) or outcome not in ("broadcast", "not_broadcast"):
            raise ControlError("action must be a number and outcome 'broadcast' or 'not_broadcast'")
        txid = body.get("txid") or None
        evidence = _text(body, "evidence", required=False)
        self._with_engine(lambda e: e.resolve_unknown_action(action, outcome, txid, evidence))
        return {"ok": True}

    def close(self):
        """Stop the active run between steps and wait for it, so the shutdown is clean."""
        self._halt.set()
        if self._thread:
            self._thread.join(timeout=30)
