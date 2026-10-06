"""
tx_builder.py -- build, verify, preview, then broadcast (as separate steps).

* build() never broadcasts. It picks the inputs itself, creates the raw
  transaction, funds it with add_inputs=False, signs it, then independently
  re-checks the decoded result: the destination must be one of OUR wallets,
  the outputs must be exactly (destination, change), the inputs must be the
  ones chosen, the fee must be under the cap, and the node must say it would
  accept it into the mempool.
* broadcast() is the only code that calls sendrawtransaction. It always goes
  through the engine's write-ahead journal, so an interrupted broadcast can
  never be retried automatically into a duplicate.
"""

import hashlib
from dataclasses import dataclass
from math import ceil

from .rpc import RpcError, to_sats


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class Prepared:
    source_wallet: str
    address: str
    amount_sats: int
    change_address: str
    hex: str
    txid: str
    fee_sats: int
    inputs: tuple
    outputs: tuple

    def summary(self):
        lines = [f"from wallet : {self.source_wallet}",
                 f"to address  : {self.address}",
                 f"amount      : {self.amount_sats} sats",
                 f"fee         : {self.fee_sats} sats",
                 f"txid        : {self.txid}",
                 f"inputs      : {len(self.inputs)}"]
        for addr, sats in self.outputs:
            lines.append(f"output      : {sats} sats -> {addr}")
        return "\n".join(lines)


class TxBuilder:
    def __init__(self, rpc, wallets, max_fee_sats=10_000_000):
        self.rpc = rpc
        self.wallets = list(wallets)
        self.max_fee_sats = int(max_fee_sats)

    def _smart_fee_rate_sat_vb(self, blocks=6):
        """Return the node's smart fee estimate in sat/vB.

        estimatesmartfee reports DGB/kvB. Convert that exact rate to sat/vB.
        If the node cannot provide a usable estimate, return None.
        """
        try:
            estimate = self.rpc.estimate_smart_fee(blocks)
            feerate = estimate.get("feerate")
            if feerate is None:
                return None
            sats_per_kvb = to_sats(feerate)
            if sats_per_kvb <= 0:
                return None
            return sats_per_kvb / 1000
        except Exception:  # noqa: BLE001
            return None

    def _fee_reserve(self, n_inputs):
        """Sats to keep back for the fee using the same smart-fee source that
        transaction funding uses. If the node cannot provide an estimate, fall
        back to the full configured fee cap."""
        rate_sat_vb = self._smart_fee_rate_sat_vb()
        if rate_sat_vb is None:
            return self.max_fee_sats
        size = 10 + 68 * n_inputs + 2 * 34
        return min(self.max_fee_sats, ceil(rate_sat_vb * size))

    def planning_fee_reserve_sats(self, n_inputs=8):
        """Conservative fee reserve used during planning.

        This is not the transaction's actual fee. It estimates relay-fee
        requirements for a moderately fragmented transaction so the planner
        does not reserve the entire max-fee safety ceiling.
        """
        if (not isinstance(n_inputs, int) or isinstance(n_inputs, bool)
                or n_inputs < 1):
            raise BuildError("planning fee input count must be a positive integer")
        return self._fee_reserve(n_inputs)

    def _owned(self, address):
        for w in self.wallets:
            try:
                if self.rpc.get_address_info(w, address).get("ismine"):
                    return True
            except RpcError:
                continue
        return False

    def build(self, source_wallet, address, amount_sats, minconf=1, fee_rate=None):
        if source_wallet not in self.wallets:
            raise BuildError("source wallet is not an experiment wallet")
        sweep = amount_sats == "all"
        if not sweep and (not isinstance(amount_sats, int) or isinstance(amount_sats, bool)
                          or amount_sats <= 0):
            raise BuildError("amount must be a positive integer number of satoshis")
        if not self._owned(address):
            raise BuildError("destination is not one of the experiment wallets; refusing")

        coins = [u for u in self.rpc.list_unspent(source_wallet, minconf)
                 if u.get("spendable", True) and u.get("safe", True)]
        coins.sort(key=lambda u: (-to_sats(u["amount"]), u["txid"], u["vout"]))
        chosen, total = [], 0
        if sweep:
            chosen, total = coins, sum(to_sats(u["amount"]) for u in coins)
            if not chosen:
                raise BuildError("nothing to send: the wallet has no confirmed spendable coins")
            request = total
            reserve = self._fee_reserve(len(chosen))
            if total <= reserve:
                raise BuildError(f"balance {total} sats does not cover the fee reserve {reserve}")
        else:
            request = amount_sats
            for u in coins:
                chosen.append(u)
                total += to_sats(u["amount"])
                if total >= amount_sats + self._fee_reserve(len(chosen)):
                    break
            reserve = self._fee_reserve(max(1, len(chosen)))
            if total < amount_sats + reserve:
                raise BuildError(f"insufficient funds: have {total} sats, need "
                                 f"{amount_sats} plus a fee reserve of {reserve}")
        value_of = {(u["txid"], u["vout"]): to_sats(u["amount"]) for u in chosen}

        change = self.rpc.get_new_address(source_wallet, "flowlab-change")
        raw = self.rpc.create_raw_transaction(
            [{"txid": u["txid"], "vout": u["vout"]} for u in chosen], {address: request})
        funded = self.rpc.fund_raw_transaction(source_wallet, raw, change, fee_rate,
                                               subtract_fee=sweep)
        signed = self.rpc.sign_raw_transaction(source_wallet, funded["hex"])
        if signed.get("complete") is not True:
            raise BuildError("transaction was not fully signed")
        hexstr = signed["hex"]

        dec = self.rpc.decode_raw_transaction(hexstr)
        ins = {(i["txid"], i["vout"]) for i in dec["vin"]}
        if ins != set(value_of):
            raise BuildError("transaction inputs are not the ones that were chosen")
        outs = []
        for o in dec["vout"]:
            outs.append((o["scriptPubKey"].get("address"), to_sats(o["value"])))
        dest = [o for o in outs if o[0] == address]
        others = [o for o in outs if o[0] != address]
        if sweep:
            if len(dest) != 1 or others:
                raise BuildError("a full-balance send must have exactly one output: the destination")
            amount_sats = dest[0][1]
        elif len(dest) != 1 or dest[0][1] != amount_sats:
            raise BuildError("destination output is not exactly the requested amount")
        if len(others) > 1 or any(a != change for a, _ in others):
            raise BuildError("unexpected output (only destination and our own change allowed)")
        fee = sum(value_of.values()) - sum(v for _, v in outs)
        if fee < 0 or fee > self.max_fee_sats:
            raise BuildError(f"fee {fee} sats is outside the allowed range 0..{self.max_fee_sats}")
        verdict = self.rpc.test_mempool_accept(hexstr)[0]
        if verdict.get("allowed") is not True:
            raise BuildError(f"node would reject it: {verdict.get('reject-reason')}")
        return Prepared(source_wallet, address, amount_sats, change, hexstr, dec["txid"], fee,
                        tuple(sorted(ins)), tuple(outs))


def broadcast(engine, rpc, job_id, prepared):
    """The ONLY path to sendrawtransaction. Journal first, send second, record third."""
    payload = {"amount_sats": prepared.amount_sats, "address": prepared.address,
               "source_wallet": prepared.source_wallet, "expected_txid": prepared.txid,
               "fee_sats": prepared.fee_sats,
               "tx_sha256": hashlib.sha256(prepared.hex.encode()).hexdigest()}
    action_id = engine.begin_action(job_id, "broadcast", payload)
    try:
        txid = rpc.send_raw_transaction(prepared.hex)
    except RpcError as e:
        definite = e.code == -26 and "already" not in str(e).lower()
        if definite:
            engine.fail_action(action_id, str(e))
        # Anything else (timeout, dropped connection, "already known") may mean the
        # node HAS the transaction: leave the intent in place for recovery to settle.
        raise
    engine.complete_action(action_id, {
        "txid": txid,
        "fee_sats": prepared.fee_sats,
        "amount_sats": prepared.amount_sats,
        "address": prepared.address,
    })
    if txid != prepared.txid:
        raise BuildError(f"node returned txid {txid}, expected {prepared.txid} (recorded as sent)")
    return txid
