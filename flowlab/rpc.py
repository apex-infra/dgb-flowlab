"""
rpc.py -- narrow DigiByte Core RPC client for dgb-flowlab.

Rules (binding):
  * Named functions only. There is NO generic passthrough; the single
    low-level call is private and refuses any method not in ALLOWED_METHODS.
  * Wallet-scoped calls only reach wallets on the allowlist (flab_* names
    given at construction). Anything else raises before a request is made.
  * Localhost only. Credentials are never logged and never put in errors.
  * Amounts are integer satoshis at this boundary; DGB floats from the node
    are converted with Decimal.
"""

import base64
import json
import re
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_EVEN

ALLOWED_METHODS = frozenset("""
getblockcount getblockchaininfo getblockhash getblockheader getnetworkinfo uptime
estimatesmartfee getmempoolinfo listwallets listwalletdir getwalletinfo getbalances
getbalance getnewaddress getaddressinfo validateaddress listunspent lockunspent
listlockunspent listtransactions gettransaction getrawtransaction decoderawtransaction
testmempoolaccept createrawtransaction fundrawtransaction signrawtransactionwithwallet
sendrawtransaction
""".split())

WALLET_NAME_RE = re.compile(r"^flab_[a-z0-9_]{1,32}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
SATS = Decimal("100000000")


class RpcError(Exception):
    """Any RPC failure. Message never contains credentials."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class WalletNotAllowed(RpcError):
    pass


class MethodNotAllowed(RpcError):
    pass


def to_sats(amount):
    return int((Decimal(str(amount)) * SATS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def to_dgb(sats):
    if not isinstance(sats, int) or isinstance(sats, bool):
        raise RpcError("amount must be integer satoshis")
    return (Decimal(sats) / SATS).quantize(Decimal("0.00000001"))


def _dumps(obj):
    # Decimal-safe JSON: render Decimals as exact numeric literals.
    def enc(o):
        if isinstance(o, Decimal):
            return "\0" + format(o, "f") + "\0"
        raise TypeError(type(o).__name__)
    s = json.dumps(obj, default=enc)
    return re.sub(r'"\\u0000([0-9.\-]+)\\u0000"', r"\1", s)


class RpcClient:
    def __init__(self, user, password, port=14022, host="127.0.0.1", timeout=30,
                 allowed_wallets=()):
        if host not in LOCAL_HOSTS:
            raise RpcError("localhost only")
        for w in allowed_wallets:
            if not WALLET_NAME_RE.match(w):
                raise WalletNotAllowed(f"wallet name not allowed: {w!r}")
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._url = f"http://{host}:{int(port)}"
        self._timeout = timeout
        self._wallets = frozenset(allowed_wallets)

    @property
    def allowed_wallets(self):
        """Immutable wallet allowlist configured for this RPC client."""
        return self._wallets

    def __repr__(self):
        return f"<RpcClient {self._url} wallets={sorted(self._wallets)}>"

    # -- private plumbing -------------------------------------------------
    def _call(self, method, params=(), wallet=None):
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(f"method not allowed: {method}")
        url = self._url
        if wallet is not None:
            if wallet not in self._wallets:
                raise WalletNotAllowed(f"wallet not allowed: {wallet!r}")
            url += "/wallet/" + wallet
        body = _dumps({"jsonrpc": "1.0", "id": "flowlab", "method": method,
                       "params": list(params)}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": self._auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                payload = json.loads(r.read().decode(), parse_float=Decimal)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode(), parse_float=Decimal)
            except Exception:  # noqa: BLE001
                raise RpcError(f"{method}: HTTP {e.code}") from None
        except Exception as e:  # noqa: BLE001
            raise RpcError(f"{method}: {type(e).__name__}") from None
        err = payload.get("error")
        if err:
            raise RpcError(f"{method}: {err.get('message')}", err.get("code"))
        return payload.get("result")

    # -- node (non-wallet) ------------------------------------------------
    def get_block_count(self):
        return self._call("getblockcount")

    def get_blockchain_info(self):
        return self._call("getblockchaininfo")

    def get_network_info(self):
        return self._call("getnetworkinfo")

    def list_wallets(self):
        return self._call("listwallets")

    def estimate_smart_fee(self, blocks=6):
        return self._call("estimatesmartfee", [int(blocks)])

    def decode_raw_transaction(self, hexstr):
        return self._call("decoderawtransaction", [hexstr])

    def test_mempool_accept(self, hexstr):
        return self._call("testmempoolaccept", [[hexstr]])

    def get_raw_transaction(self, txid, verbose=True):
        return self._call("getrawtransaction", [txid, bool(verbose)])

    # -- wallet-scoped ----------------------------------------------------
    def get_wallet_info(self, wallet):
        return self._call("getwalletinfo", wallet=wallet)

    def get_balances(self, wallet):
        return self._call("getbalances", wallet=wallet)

    def get_new_address(self, wallet, label=""):
        return self._call("getnewaddress", [label], wallet=wallet)

    def get_address_info(self, wallet, address):
        return self._call("getaddressinfo", [address], wallet=wallet)

    def list_unspent(self, wallet, minconf=0):
        return self._call("listunspent", [int(minconf)], wallet=wallet)

    def get_transaction(self, wallet, txid):
        return self._call("gettransaction", [txid], wallet=wallet)

    def create_raw_transaction(self, inputs, outputs):
        """outputs: {address: sats(int)}. Not wallet-scoped."""
        out = {a: to_dgb(s) for a, s in outputs.items()}
        return self._call("createrawtransaction", [inputs, out])

    def fund_raw_transaction(self, wallet, hexstr, change_address, fee_rate_sat_vb=None,
                             subtract_fee=False):
        opts = {"add_inputs": False, "changeAddress": change_address}
        if subtract_fee:
            opts["subtractFeeFromOutputs"] = [0]
        if fee_rate_sat_vb is not None:
            opts["fee_rate"] = fee_rate_sat_vb
        return self._call("fundrawtransaction", [hexstr, opts], wallet=wallet)

    def sign_raw_transaction(self, wallet, hexstr):
        return self._call("signrawtransactionwithwallet", [hexstr], wallet=wallet)

    def lock_unspent(self, wallet, lock, outputs):
        return self._call("lockunspent", [bool(lock), outputs], wallet=wallet)

    def send_raw_transaction(self, hexstr):
        """Broadcast. Only tx_builder.Broadcaster may call this, via the journal."""
        return self._call("sendrawtransaction", [hexstr])
