"""A tiny simulated chain that implements the RpcClient methods the engine uses."""
import hashlib
from decimal import Decimal

from flowlab.rpc import RpcError

FEE = 1_410_252
SATS = Decimal(10) ** 8


class FakeChain:
    def __init__(self, wallets):
        self.wallets = list(wallets)
        self.height = 100
        self.owner, self.utxos, self.txs, self.raw = {}, {}, {}, {}
        self.n = 0
        self.fail_send = None          # set to an RpcError to make broadcasts fail

    def _addr(self, wallet):
        self.n += 1
        a = f"dgb1q{wallet}{self.n}"
        self.owner[a] = wallet
        return a

    def fund(self, wallet, sats):
        a = self._addr(wallet)
        txid = hashlib.sha256(f"fund{self.n}".encode()).hexdigest()
        self.utxos[(txid, 0)] = {"addr": a, "sats": sats, "spent": False, "txid": txid}
        self.txs[txid] = {"inputs": [], "outputs": [(a, sats)], "height": self.height}

    def mine(self, n=1):
        self.height += n
        for t in self.txs.values():
            if t["height"] is None:
                t["height"] = self.height - n + 1

    def _confs(self, txid):
        h = self.txs[txid]["height"]
        return 0 if h is None else self.height - h + 1

    # -- RpcClient surface ------------------------------------------------
    def get_blockchain_info(self):
        return {"blocks": self.height, "headers": self.height, "initialblockdownload": False}

    def get_network_info(self):
        return {"relayfee": Decimal("0.1")}

    def list_wallets(self):
        return list(self.wallets)

    def get_new_address(self, wallet, label=""):
        return self._addr(wallet)

    def get_address_info(self, wallet, address):
        return {"ismine": self.owner.get(address) == wallet}

    def list_unspent(self, wallet, minconf=0):
        return [{"txid": u["txid"], "vout": v, "amount": Decimal(u["sats"]) / SATS,
                 "spendable": True, "safe": True}
                for (t, v), u in self.utxos.items()
                if not u["spent"] and self.owner[u["addr"]] == wallet and self._confs(t) >= minconf]

    def get_balances(self, wallet):
        tr = sum(u["sats"] for (t, v), u in self.utxos.items()
                 if not u["spent"] and self.owner[u["addr"]] == wallet and self._confs(t) >= 1)
        pe = sum(u["sats"] for (t, v), u in self.utxos.items()
                 if not u["spent"] and self.owner[u["addr"]] == wallet and self._confs(t) == 0)
        return {"mine": {"trusted": Decimal(tr) / SATS, "untrusted_pending": Decimal(pe) / SATS}}

    def create_raw_transaction(self, inputs, outputs):
        self.n += 1
        h = f"raw{self.n}"
        self.raw[h] = {"inputs": [(i["txid"], i["vout"]) for i in inputs], "outputs": dict(outputs)}
        return h

    def fund_raw_transaction(self, wallet, hexstr, change_address, fee_rate=None, subtract_fee=False):
        r = self.raw[hexstr]
        total = sum(self.utxos[i]["sats"] for i in r["inputs"])
        if subtract_fee:
            k = next(iter(r["outputs"]))
            r["outputs"][k] -= FEE
        out = sum(r["outputs"].values())
        change = total - out - FEE
        if subtract_fee:
            change = total - out - FEE if total - out > FEE else 0
        if change < 0:
            raise RpcError("Insufficient funds", -4)
        self.n += 1
        h = f"fund{self.n}"
        outs = list(r["outputs"].items()) + ([(change_address, change)] if change else [])
        self.raw[h] = {"inputs": r["inputs"], "outputs": dict(outs), "ordered": outs}
        return {"hex": h, "fee": Decimal(FEE) / SATS}

    def sign_raw_transaction(self, wallet, hexstr):
        return {"hex": hexstr, "complete": True}

    def decode_raw_transaction(self, hexstr):
        r = self.raw[hexstr]
        return {"txid": hashlib.sha256(hexstr.encode()).hexdigest(),
                "vin": [{"txid": t, "vout": v} for t, v in r["inputs"]],
                "vout": [{"value": Decimal(s) / SATS, "scriptPubKey": {"address": a}}
                         for a, s in r["ordered"]]}

    def test_mempool_accept(self, hexstr):
        return [{"allowed": True}]

    def send_raw_transaction(self, hexstr):
        if self.fail_send:
            raise self.fail_send
        r = self.raw[hexstr]
        for i in r["inputs"]:
            if self.utxos[i]["spent"]:
                raise RpcError("Missing inputs", -25)
        txid = hashlib.sha256(hexstr.encode()).hexdigest()
        ins = [(self.utxos[i]["addr"], self.utxos[i]["sats"]) for i in r["inputs"]]
        for i in r["inputs"]:
            self.utxos[i]["spent"] = True
        for v, (a, s) in enumerate(r["ordered"]):
            self.utxos[(txid, v)] = {"addr": a, "sats": s, "spent": False, "txid": txid}
        self.txs[txid] = {"inputs": ins, "outputs": r["ordered"], "height": None}
        return txid

    def get_raw_transaction(self, txid, verbose=True):
        if txid not in self.txs:
            raise RpcError("No such mempool or blockchain transaction", -5)
        return {"txid": txid}

    def get_transaction(self, wallet, txid):
        t = self.txs.get(txid)
        mine_in = sum(s for a, s in (t or {"inputs": []})["inputs"] if self.owner[a] == wallet)
        mine_out = sum(s for a, s in (t or {"outputs": []})["outputs"] if self.owner[a] == wallet)
        if t is None or (mine_in == 0 and mine_out == 0):
            raise RpcError("Invalid or non-wallet transaction id", -5)
        net = mine_out - mine_in
        res = {"confirmations": self._confs(txid), "blockheight": t["height"]}
        if mine_in:
            res["fee"] = -Decimal(FEE) / SATS
            res["amount"] = Decimal(net + FEE) / SATS
        else:
            res["amount"] = Decimal(net) / SATS
        return res
