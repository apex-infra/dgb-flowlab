"""Build and verify a 1 DGB transfer flab_source -> flab_a. NEVER broadcasts."""
import json
from flowlab.rpc import RpcClient
from flowlab.tx_builder import TxBuilder

cfg = json.load(open("config.local.json"))
c = RpcClient(cfg["rpc_user"], cfg["rpc_password"], cfg["rpc_port"],
              cfg["rpc_host"], allowed_wallets=cfg["wallets"])
b = TxBuilder(c, cfg["wallets"], max_fee_sats=10_000_000)
addr = c.get_new_address("flab_a", "flowlab-preview")
p = b.build("flab_source", addr, 100_000_000)
print(p.summary())
print("\nPREVIEW ONLY - nothing was broadcast.")
