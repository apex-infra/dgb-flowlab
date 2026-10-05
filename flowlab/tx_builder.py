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
    def __init__(self, rpc, wallets, max_fee_sats=1_000_000):
        self.rpc = rpc
        self.wallets = list(wallets)
        self.max_fee_sats = int(max_fee_sats)

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
        if not isinstance(amount_sats, int) or isinstance(amount_sats, bool) or amount_sats <= 0:
            raise BuildError("amount must be a positive integer number of satoshis")
        if not self._owned(address):
            raise BuildError("destination is not one of the experiment wallets; refusing")

        coins = [u for u in self.rpc.list_unspent(source_wallet, minconf)
                 if u.get("spendable", True) and u.get("safe", True)]
        coins.sort(key=lambda u: (-to_sats(u["amount"]), u["txid"], u["vout"]))
        chosen, total = [], 0
        for u in coins:
            chosen.append(u)
            total += to_sats(u["amount"])
            if total >= amount_sats + self.max_fee_sats:
                break
        if total < amount_sats + self.max_fee_sats:
            raise BuildError(f"insufficient funds: have {total} sats, need "
                             f"{amount_sats} plus a fee reserve of {self.max_fee_sats}")
        value_of = {(u["txid"], u["vout"]): to_sats(u["amount"]) for u in chosen}

        change = self.rpc.get_new_address(source_wallet, "flowlab-change")
        raw = self.rpc.create_raw_transaction(
            [{"txid": u["txid"], "vout": u["vout"]} for u in chosen], {address: amount_sats})
        funded = self.rpc.fund_raw_transaction(source_wallet, raw, change, fee_rate)
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
        if len(dest) != 1 or dest[0][1] != amount_sats:
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
    engine.complete_action(action_id, {"txid": txid, "fee_sats": prepared.fee_sats})
    if txid != prepared.txid:
        raise BuildError(f"node returned txid {txid}, expected {prepared.txid} (recorded as sent)")
    return txid
