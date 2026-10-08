"""
node_verifier.py -- the real Verifier, backed by the local node (read-only calls).

Every required check is positively established from the node; any doubt is a
failure. `known_txids(flow)` lets the caller supply txids the engine has
recorded for the flow, so they can be checked against the node.
"""

import json

from .config_schema import (
    destination_identity,
    destination_is_external,
    destination_wallets,
    has_multi_destinations,
)
from .rpc import RpcError, to_sats
from .states import FlowState
from .verify import Verifier


class NodeVerifier(Verifier):
    def __init__(self, rpc, known_txids=None, min_peers=1):
        self.rpc = rpc
        self.known_txids = known_txids or (lambda flow: [])
        self.min_peers = min_peers
        self.allowed_wallets = frozenset(getattr(rpc, "allowed_wallets", ()))

    def verify(self, experiment, flow, action):
        checks = {}
        for name, fn in (("height", self._height), ("wallet", self._wallet),
                         ("balance", self._balance), ("utxo", self._utxo),
                         ("transaction", self._transaction),
                         ("confirmation", self._confirmation),
                         ("flow_state", self._flow_state)):
            try:
                checks[name] = fn(experiment, flow)
            except Exception as e:  # noqa: BLE001 - fail closed
                checks[name] = (False, f"{type(e).__name__}: {e}")
        return checks

    @staticmethod
    def _config_flow(experiment, flow):
        if not experiment or not flow:
            return None

        raw = experiment.get("config_json")
        if not raw:
            return None

        cfg = json.loads(raw)

        matches = [
            fl for fl in cfg.get("flows", [])
            if fl.get("source_wallet") == flow["source_wallet"]
            and destination_identity(fl) == flow["destination_wallet"]
        ]

        return matches[0] if len(matches) == 1 else None

    def _wallets(self, experiment, flow):
        if not flow:
            return []

        wallets = [
            flow["source_wallet"],
            *json.loads(flow["flow_wallets_json"]),
        ]

        cfg_flow = self._config_flow(experiment, flow)

        settlement_cycle = False

        if cfg_flow is not None and experiment:
            raw = experiment.get("config_json")

            if raw:
                cfg = json.loads(raw)
                play = cfg.get("play") or {}

                settlement_cycle = (
                    play.get("name") == "settlement_cycle"
                    and play.get("version") == 1
                )

        # Fail closed if the DB flow cannot be matched to its immutable
        # approved config. Synthetic identities are never treated as wallet
        # names once their config has been positively matched.
        if cfg_flow is None:
            wallets.append(flow["destination_wallet"])
        elif settlement_cycle:
            # Reserve, Stage, hubs, and workers are already represented by
            # source_wallet + flow_wallets_json. Settlement Cycle has no
            # terminal destination wallet, but internal settlement payout
            # wallets are still real Core wallets and must be verified.
            cfg = json.loads(
                experiment["config_json"]
            )
            settlement = (
                cfg.get("settlement_cycle", {})
                .get("settlement", {})
            )

            for item in settlement.get("items", []):
                if item.get("type") == "wallet":
                    wallets.append(item["wallet"])
        elif has_multi_destinations(cfg_flow):
            wallets.extend(destination_wallets(cfg_flow))
        elif not destination_is_external(cfg_flow):
            wallets.append(flow["destination_wallet"])

        return list(dict.fromkeys(wallets))

    def _height(self, exp, flow):
        info = self.rpc.get_blockchain_info()
        if info.get("initialblockdownload"):
            return (False, "node in initial block download")
        if info["blocks"] < info["headers"] - 1:
            return (False, f"node behind headers ({info['blocks']}/{info['headers']})")
        return (True, f"height {info['blocks']}")

    def _wallet(self, exp, flow):
        loaded = set(self.rpc.list_wallets())
        want = self._wallets(exp, flow)
        if not want:
            return (True, "no flow")
        # Required flow wallets must be loaded. Additional loaded wallets are
        # permitted only when they belong to FlowLab's configured RPC allowlist.
        allowed = self.allowed_wallets or set(want)
        extra = sorted(loaded - set(allowed))
        if extra:
            return (False, f"non-allowlisted wallets loaded: {extra}")
        missing = [w for w in want if w not in loaded]
        if missing:
            return (False, f"wallets not loaded: {missing}")
        return (True, f"{len(want)} wallets loaded")

    def _balance(self, exp, flow):
        if not flow:
            return (True, "no flow")
        b = self.rpc.get_balances(flow["source_wallet"])["mine"]
        if flow["state"] == FlowState.START.value:
            have = to_sats(b["trusted"])
            if have < flow["initial_alloc_sats"]:
                return (False, f"source has {have} sats, needs {flow['initial_alloc_sats']}")
        return (True, "balance sufficient")

    def _utxo(self, exp, flow):
        total = 0
        for w in self._wallets(exp, flow):
            for u in self.rpc.list_unspent(w, 0):
                if to_sats(u["amount"]) <= 0:
                    return (False, f"bad utxo in {w}")
                total += 1
        return (True, f"{total} utxos readable")

    def _lookup(self, exp, flow, txid):
        """Wallet-side lookup, so it works without -txindex."""
        for w in self._wallets(exp, flow):
            try:
                return self.rpc.get_transaction(w, txid)
            except RpcError:
                continue
        return None

    def _transaction(self, exp, flow):
        for txid in self.known_txids(flow):
            if self._lookup(exp, flow, txid) is None:
                return (False, f"{txid[:12]} is not known to any flow wallet")
        return (True, "recorded txids known to node")

    def _confirmation(self, exp, flow):
        for txid in self.known_txids(flow):
            t = self._lookup(exp, flow, txid)
            if t is not None and t.get("confirmations", 0) < 0:
                return (False, f"{txid[:12]} conflicted")
        return (True, "confirmation state consistent")

    def _flow_state(self, exp, flow):
        if not flow:
            return (True, "no flow")
        FlowState(flow["state"])
        if flow["error_state"] != "NONE":
            return (False, f"flow error state {flow['error_state']}")
        return (True, flow["state"])
