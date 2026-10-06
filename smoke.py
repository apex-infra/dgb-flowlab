"""Read-only smoke test against the real node. Changes nothing, sends nothing."""
import json
from flowlab import rpc
from flowlab.rpc import RpcClient, RpcError

cfg = json.load(open("config.local.json"))
c = RpcClient(cfg["rpc_user"], cfg["rpc_password"], cfg["rpc_port"],
              cfg["rpc_host"], allowed_wallets=cfg["wallets"])
bad = 0


def show(label, fn):
    global bad
    try:
        print("OK   ", label, "->", fn())
    except Exception as e:  # noqa: BLE001
        bad += 1
        print("FAIL ", label, "->", type(e).__name__, e)


info = {}
show("block height", c.get_block_count)
show("chain info", lambda: (lambda i: (info.update(i), (i["chain"], i["blocks"], i["headers"],
                            "IBD" if i["initialblockdownload"] else "synced"))[1])(c.get_blockchain_info()))
show("loaded wallets", c.list_wallets)
for w in cfg["wallets"]:
    show("balance " + w, lambda w=w: str(c.get_balances(w)["mine"]["trusted"]) + " DGB")

# Prove the NODE (not just our code) refuses a method outside the whitelist.
rpc.ALLOWED_METHODS = rpc.ALLOWED_METHODS | {"getpeerinfo"}
try:
    c._call("getpeerinfo")
    bad += 1
    print("FAIL  node allowed getpeerinfo - whitelist is NOT enforced")
except RpcError as e:
    print("OK    node refused a non-whitelisted method:", e)

print("\nRESULT:", "ALL GOOD" if bad == 0 else f"{bad} PROBLEM(S)")
