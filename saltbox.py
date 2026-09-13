#!/usr/bin/env python3
"""
Saltgrain saltbox (盐框). Standard library only.

    python3 saltbox.py new                       create a key pair
    python3 saltbox.py address                   show your address
    python3 saltbox.py balance                   show confirmed balance
    python3 saltbox.py send --to ADDR --amount 1.5 --fee 0.001

`send` prints a signed transaction; paste it as a comment on the mempool
issue and a miner will pick it up.

The private key is stored in plain text in saltbox.json. Saltgrain has no value and these
worth nothing and this key must never be reused anywhere that matters.
"""

import argparse
import base64
import json
import os
import secrets
import stat
import sys

from saltgrain import chain as chainmod
from saltgrain import crypto
from saltgrain.consensus import COIN, ConsensusError, Tx, TxIn, TxOut, format_amount, validate_tx

SALTBOX = "saltbox.json"
PREFIX = "salt-tx-v1:"


def load_saltbox(path=SALTBOX):
    if not os.path.exists(path):
        raise SystemExit(f"no saltbox at {path}. run:  python3 saltbox.py new")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_new(args):
    if os.path.exists(args.saltbox) and not args.force:
        raise SystemExit(f"{args.saltbox} already exists. use --force to overwrite it.")
    priv_bytes = secrets.token_bytes(32)
    priv = crypto.privkey_from_bytes(priv_bytes)
    pub = crypto.ser_pubkey(crypto.pubkey(priv))
    address = crypto.pubkey_to_address(pub)
    data = {
        "version": 1,
        "privkey": priv_bytes.hex(),
        "pubkey": pub.hex(),
        "address": address,
        "warning": "Saltgrain toy key. Worth nothing. Never reuse this key.",
    }
    with open(args.saltbox, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(args.saltbox, stat.S_IRUSR | stat.S_IWUSR)
    print(f"saltbox written to {args.saltbox}")
    print(f"address  {address}")


def cmd_address(args):
    print(load_saltbox(args.saltbox)["address"])


def _state(args):
    return chainmod.replay(chainmod.load_blocks(), strict_time=False)


def cmd_balance(args):
    w = load_saltbox(args.saltbox)
    state = _state(args)
    mine = [
        (k, v) for k, v in state.utxos.utxos.items() if v["address"] == w["address"]
    ]
    total = sum(v["value"] for _, v in mine)
    print(f"address  {w['address']}")
    print(f"balance  {format_amount(total)} SALT across {len(mine)} output(s)")
    for (txid, vout), v in sorted(mine, key=lambda p: -p[1]["value"]):
        tag = " (coinbase)" if v["coinbase"] else ""
        print(f"  {txid[:16]}…:{vout}  {format_amount(v['value']):>18} SALT  "
              f"height {v['height']}{tag}")


def parse_amount(s: str) -> int:
    """Convert a decimal SALT string into an integer number of motes."""
    if "." not in s:
        return int(s) * COIN
    whole, frac = s.split(".", 1)
    if len(frac) > 8:
        raise SystemExit("amounts have at most 8 decimal places")
    return int(whole or 0) * COIN + int(frac.ljust(8, "0"))


def cmd_send(args):
    w = load_saltbox(args.saltbox)
    state = _state(args)
    height = state.height + 1

    if not crypto.address_is_valid(args.to):
        raise SystemExit(f"invalid destination address: {args.to}")

    amount = parse_amount(args.amount)
    fee = parse_amount(args.fee)
    need = amount + fee

    spendable = []
    for (txid, vout), v in state.utxos.utxos.items():
        if v["address"] != w["address"]:
            continue
        from saltgrain.consensus import COINBASE_MATURITY

        if v["coinbase"] and height - v["height"] < COINBASE_MATURITY:
            continue
        spendable.append((txid, vout, v["value"]))
    spendable.sort(key=lambda t: -t[2])

    picked, total = [], 0
    for txid, vout, value in spendable:
        picked.append((txid, vout, value))
        total += value
        if total >= need:
            break
    if total < need:
        raise SystemExit(
            f"insufficient mature funds: have {format_amount(total)}, "
            f"need {format_amount(need)} SALT"
        )

    outputs = [TxOut(amount, args.to)]
    change = total - need
    if change > 0:
        outputs.append(TxOut(change, w["address"]))

    tx = Tx(
        inputs=[TxIn(txid, vout, w["pubkey"], "") for txid, vout, _ in picked],
        outputs=outputs,
        memo=args.memo,
    )
    digest = tx.sighash()
    priv = crypto.privkey_from_bytes(bytes.fromhex(w["privkey"]))
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig

    try:
        validate_tx(tx, state.utxos, height)
    except ConsensusError as exc:
        raise SystemExit(f"refusing to emit an invalid transaction: {exc}")

    payload = base64.b64encode(
        json.dumps(tx.to_dict(), separators=(",", ":"), sort_keys=True).encode()
    ).decode()

    print(f"txid    {tx.txid()}")
    print(f"send    {format_amount(amount)} SALT -> {args.to}")
    print(f"fee     {format_amount(fee)} SALT")
    if args.memo:
        print(f'memo    "{args.memo}"')
    if change:
        print(f"change  {format_amount(change)} SALT -> {w['address']}")
    print()
    print("Paste this as a comment on the mempool issue:")
    print()
    print(PREFIX + payload)


def cmd_identity(args):
    """
    Prove that one GitHub handle and one Saltgrain address are the same person.

    The signature proves you hold the key; posting it from your account
    proves you hold the handle. Both directions are needed, and neither is
    consensus -- it only decides whose name appears next to a balance.
    """
    w = load_saltbox(args.saltbox)
    priv = crypto.privkey_from_bytes(bytes.fromhex(w["privkey"]))
    digest = crypto.sha256d(b"saltgrain-identity-v1|" + args.handle.encode())
    sig = crypto.sign(priv, digest).hex()
    print(f"address  {w['address']}")
    print(f"handle   {args.handle}")
    print()
    print("Post this from the GitHub account it names:")
    print()
    print(f"salt-id-v1:{args.handle}:{w['pubkey']}:{sig}")


def main():
    ap = argparse.ArgumentParser(description="Saltgrain saltbox (盐框)")
    ap.add_argument("--saltbox", default=SALTBOX)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="generate a new key pair")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new)

    sub.add_parser("address", help="print your address").set_defaults(func=cmd_address)
    sub.add_parser("balance", help="print your balance").set_defaults(func=cmd_balance)

    p = sub.add_parser("identity", help="link your GitHub handle to your address")
    p.add_argument("--handle", required=True, help="your GitHub username")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("send", help="build and sign a transaction")
    p.add_argument("--to", required=True)
    p.add_argument("--amount", required=True, help="in SALT, e.g. 1.5")
    p.add_argument("--fee", default="0.001", help="in SALT")
    p.add_argument("--memo", default="", help="a note to attach, max 120 bytes")
    p.set_defaults(func=cmd_send)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
