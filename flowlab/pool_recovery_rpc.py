"""Restricted DigiByte RPC interface for Mixer Lab evidence recovery.

This interface supports blockchain observation only.
It intentionally does not expose transaction construction,
signing, wallet transfers, or broadcasting.
"""


class RecoveryRpc:
    __slots__ = ("__client",)

    def __init__(self, client):
        required = (
            "list_transactions",
            "get_block_hash",
            "get_block_header",
            "get_raw_transaction",
        )

        for name in required:
            if not callable(getattr(client, name, None)):
                raise TypeError(f"Recovery RPC requires {name}")

        self.__client = client

    def list_transactions(self, wallet, **kwargs):
        return self.__client.list_transactions(wallet, **kwargs)

    def get_block_hash(self, height):
        return self.__client.get_block_hash(height)

    def get_block_header(self, blockhash):
        return self.__client.get_block_header(blockhash)

    def get_raw_transaction(self, txid, verbose=True, blockhash=None):
        return self.__client.get_raw_transaction(
            txid,
            verbose=verbose,
            blockhash=blockhash,
        )


def create_recovery_rpc(
    *,
    user,
    password,
    port=14022,
    host="127.0.0.1",
    allowed_wallets=(),
    timeout=30,
):
    """Build a bounded, localhost-only observation RPC client."""
    from .rpc import RpcClient

    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError("Recovery RPC timeout must be 1-30 seconds")

    if not allowed_wallets:
        raise ValueError("Recovery RPC requires an explicit wallet allowlist")

    client = RpcClient(
        user,
        password,
        port=port,
        host=host,
        timeout=timeout,
        allowed_wallets=allowed_wallets,
    )

    return RecoveryRpc(client)
