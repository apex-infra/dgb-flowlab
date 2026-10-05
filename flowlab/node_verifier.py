"""
node_verifier.py -- the real Verifier, backed by the local node (read-only calls).

Every required check is positively established from the node; any doubt is a
failure. `known_txids(flow)` lets the caller supply txids the engine has
recorded for the flow, so they can be checked against the node.
"""

import json

from .rpc import RpcError, to_sats
from .states import FlowState
from .verify import Verifier


class NodeVerifier(Verifier):
    def __init__(self, rpc, known_txids=None, min_peers=1):
        self.rpc = rpc
        self.known_txids = known_txids or (lambda flow: [])
        self.min_peers = min_peers

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
    def _wallets(flow):
        if not flow:
            return []
        w = [flow["source_wallet"], *json.loads(flow["flow_wallets_json"]),
             flow["destination_wallet"]]
        return list(dict.fromkeys(w))

    def _height(self, exp, flow):
        info = self.rpc.get_blockchain_info()
        if info.get("initialblockdownload"):
            return (False, "node in initial block download")
        if info["blocks"] < info["headers"] - 1:
            return (False, f"node behind headers ({info['blocks']}/{info['headers']})")
        return (True, f"height {info['blocks']}")

    def _wallet(self, exp, flow):
        loaded = set(self.rpc.list_wallets())
        want = self._wallets(flow)
        if not want:
            return (True, "no flow")
        extra = sorted(loaded - set(want))
        if extra:
            return (False, f"unexpected wallets loaded: {extra}")
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
        for w in self._wallets(flow):
            for u in self.rpc.list_unspent(w, 0):
                if to_sats(u["amount"]) <= 0:
                    return (False, f"bad utxo in {w}")
                total += 1
        return (True, f"{total} utxos readable")

    def _lookup(self, flow, txid):
        """Wallet-side lookup, so it works without -txindex."""
        for w in self._wallets(flow):
            try:
                return self.rpc.get_transaction(w, txid)
            except RpcError:
                continue
        return None

    def _transaction(self, exp, flow):
        for txid in self.known_txids(flow):
            if self._lookup(flow, txid) is None:
                return (False, f"{txid[:12]} is not known to any flow wallet")
        return (True, "recorded txids known to node")

    def _confirmation(self, exp, flow):
        for txid in self.known_txids(flow):
            t = self._lookup(flow, txid)
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
