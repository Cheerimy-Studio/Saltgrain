#!/usr/bin/env python3
"""
End-to-end consensus tests for Saltgrain (盐粒).

Runs at the chain's easiest allowed difficulty so the whole suite takes a
few seconds. Exercises a real spend with real signatures, then tries to
break the chain eight different ways and asserts each attempt is rejected.

    python3 tests/test_chain.py
"""

import copy
import hashlib
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saltgrain import chain as chainmod  # noqa: E402
from saltgrain import consensus as k  # noqa: E402
from saltgrain import crypto  # noqa: E402
from saltgrain import pow as rp  # noqa: E402

# Shrink the puzzle so the suite runs in seconds. The live chain uses n=40;
# n=16 exercises exactly the same code paths at 2^8 instead of 2^20 work.
rp.N = 16
rp.B = rp.N - 2
rp.UNIT_WORK = 1 << (rp.N // 2)
k.POW_LIMIT_BITS = 0x1F010000
k.GENESIS_BITS = k.POW_LIMIT_BITS

PASSED = []
BASE_TIME = 1_750_000_000


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def expect_reject(label, fn):
    """Assert that `fn` raises ConsensusError, and show the message."""
    try:
        fn()
    except k.ConsensusError as exc:
        msg = str(exc)
        short = msg if len(msg) < 88 else msg[:85] + "…"
        print(f"  ok    {label}\n          rejected: {short}")
        PASSED.append(label)
        return
    raise AssertionError(f"FAILED: {label} was accepted but should have been rejected")


def new_key():
    priv_bytes = secrets.token_bytes(32)
    priv = crypto.privkey_from_bytes(priv_bytes)
    pub = crypto.ser_pubkey(crypto.pubkey(priv))
    return priv, pub.hex(), crypto.pubkey_to_address(pub)


def mine_block(blocks, miner, address, txs=None, message="", timestamp=None):
    """Build and mine a valid block extending `blocks`."""
    txs = txs or []
    height = len(blocks)
    state = chainmod.replay(blocks, strict_time=False)
    bits = chainmod.bits_for_height(height, blocks)

    fees = 0
    working = state.utxos.copy()
    for t in txs:
        fees += k.validate_tx(t, working, height)
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)

    coinbase = k.Tx(
        coinbase=message,
        cb_height=height,
        outputs=[k.TxOut(k.block_subsidy(height) + fees, address)],
    )
    all_txs = [coinbase] + txs
    ts = timestamp if timestamp is not None else BASE_TIME + height * 600

    block = k.Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=k.merkle_root([t.txid() for t in all_txs]),
        timestamp=ts,
        bits=bits,
        miner=miner,
        txs=all_txs,
    )

    core = block.header_core()
    kk = block.puzzles()
    block.solution = rp.encode_solutions(
        [rp.solve_puzzle(core, j) for j in range(kk)]
    )
    assert block.block_hash() == crypto.sha256d(block.header()).hex()
    return block


def sign_tx(tx, priv):
    digest = tx.sighash()
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig
    return tx


def run():
    now = BASE_TIME + 200 * 600
    print("Saltgrain consensus tests")
    print()

    alice_priv, alice_pub, alice_addr = new_key()
    bob_priv, bob_pub, bob_addr = new_key()

    # ---- build a chain -------------------------------------------------
    blocks = [mine_block([], "saltgrain", alice_addr, message="saltgrain genesis")]
    ok("genesis mined and self-consistent")

    for _ in range(1, 12):
        blocks.append(mine_block(blocks, "saltgrain", alice_addr))
    state = chainmod.replay(blocks, now=now)
    assert state.height == 11
    ok(f"12 blocks replay clean (height {state.height})")

    # ---- a real spend --------------------------------------------------
    gen_coinbase_txid = blocks[0].txs[0].txid()
    entry = state.utxos.get(gen_coinbase_txid, 0)
    assert entry and entry["address"] == alice_addr

    amount = 10 * k.COIN
    fee = 5000
    change = entry["value"] - amount - fee
    spend = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(amount, bob_addr), k.TxOut(change, alice_addr)],
    )
    sign_tx(spend, alice_priv)

    blocks.append(mine_block(blocks, "octocat", bob_addr, txs=[spend], message="first spend"))
    state = chainmod.replay(blocks, now=now)
    ok("block containing a signed transaction accepted")

    balances = state.utxos.balances()
    assert balances[bob_addr] == amount + k.block_subsidy(12) + fee, balances[bob_addr]
    ok(f"bob holds {k.format_amount(balances[bob_addr])} SALT (10 received + reward + fee)")

    emitted = chainmod.emitted_supply(state.height)
    assert emitted == state.circulating(), (emitted, state.circulating())
    ok(f"supply conserved: {k.format_amount(emitted)} SALT emitted == unspent")

    # ---- coinbase maturity ---------------------------------------------
    young_txid = blocks[12].txs[0].txid()
    bad_spend = k.Tx(
        inputs=[k.TxIn(young_txid, 0, bob_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(bad_spend, bob_priv)
    expect_reject(
        "immature coinbase cannot be spent",
        lambda: mine_block(blocks, "octocat", bob_addr, txs=[bad_spend]),
    )

    # ---- retarget ------------------------------------------------------
    while len(blocks) < 17:
        blocks.append(mine_block(blocks, "saltgrain", alice_addr))
    state = chainmod.replay(blocks, now=now)
    b16 = blocks[16]
    assert b16.height == 16
    assert b16.bits == chainmod.bits_for_height(16, blocks[:16])
    ok(f"difficulty retargeted at height 16 (bits {b16.bits:#010x})")

    good = list(blocks)

    # ---- tampering -----------------------------------------------------
    t = copy.deepcopy(good)
    sols = rp.decode_solutions(t[5].solution)
    sols[0] = (sols[0][0], sols[0][1] ^ 1)
    t[5].solution = rp.encode_solutions(sols)
    expect_reject("altered subset breaks proof of work", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].outputs[0].value += 1
    expect_reject("altered output breaks the merkle root", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[3].txs[0].outputs[0].value += 1
    t[3].merkle_root = k.merkle_root([x.txid() for x in t[3].txs])
    expect_reject("inflated coinbase rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].inputs[0].sig = "00" * 64
    t[12].merkle_root = k.merkle_root([x.txid() for x in t[12].txs])
    expect_reject("forged signature rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].miner = "someone-else"
    expect_reject("stealing a block by renaming the miner invalidates the puzzles",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[7].solution = t[7].solution + "0" * (2 * rp.SOLUTION_BYTES)
    expect_reject("padding the solution with an extra puzzle rejected",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    del t[8]
    for i, b in enumerate(t):
        b.height = i
    expect_reject("removing a block breaks the hash chain", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[9].bits = k.POW_LIMIT_BITS + 0x00010000
    expect_reject("mining at the wrong difficulty rejected", lambda: chainmod.replay(t, now=now))

    dbl = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(dbl, alice_priv)
    expect_reject(
        "double spend of an already-spent output rejected",
        lambda: mine_block(good, "saltgrain", alice_addr, txs=[dbl]),
    )

    stale = mine_block(good[:13], "saltgrain", alice_addr)
    expect_reject(
        "block built on a stale tip rejected",
        lambda: chainmod.replay(good + [stale], now=now),
    )

    # ---- legacy addresses ----------------------------------------------
    # Addresses minted before the rename carry HRP "rofl". They must still
    # verify and still be spendable: ownership compares the 20-byte payload,
    # not the prefix, so a rofl1… output is not orphaned by the rename.
    alice_payload = crypto.sha256(bytes.fromhex(alice_pub))[:20]
    legacy_addr = crypto.bech32_encode("rofl", [0] + crypto._convertbits(alice_payload, 8, 5))
    assert legacy_addr.startswith("rofl1") and alice_addr.startswith("salt1")
    assert crypto.address_is_valid(legacy_addr) and crypto.address_is_valid(alice_addr)
    assert crypto.same_address(legacy_addr, alice_addr)
    ok("legacy rofl1… and new salt1… addresses name the same payload")

    legacy_chain = [mine_block([], "saltgrain", legacy_addr, message="legacy payout")]
    for _ in range(10):  # mature the coinbase
        legacy_chain.append(mine_block(legacy_chain, "saltgrain", legacy_addr))
    legacy_spend = k.Tx(
        inputs=[k.TxIn(legacy_chain[0].txs[0].txid(), 0, alice_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(legacy_spend, alice_priv)
    legacy_chain.append(mine_block(legacy_chain, "saltgrain", bob_addr, txs=[legacy_spend]))
    chainmod.replay(legacy_chain, now=now)
    ok("coinbase paid to a legacy rofl1… address is still spendable")

    # ---- the star gate --------------------------------------------------
    import submit  # noqa: E402

    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    saved_urlopen = submit.urllib.request.urlopen
    saved_env = {key: os.environ.get(key) for key in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        assert submit.author_has_starred("anyone") is True, "no env must fail open"

        os.environ["GITHUB_REPOSITORY"] = "owner/repo"
        os.environ["GITHUB_TOKEN"] = "token"
        submit.urllib.request.urlopen = lambda req, timeout=30: _Resp([{"login": "Alice"}])
        assert submit.author_has_starred("alice") is True, "case-insensitive match"
        submit.urllib.request.urlopen = lambda req, timeout=30: _Resp([{"login": "bob"}])
        assert submit.author_has_starred("alice") is False, "absent stargazer rejected"

        def _boom(req, timeout=30):
            raise OSError("network down")

        submit.urllib.request.urlopen = _boom
        assert submit.author_has_starred("alice") is True, "API failure must fail open"
        ok("star gate: matches stars, rejects non-stargazers, fails open on API errors")
    finally:
        submit.urllib.request.urlopen = saved_urlopen
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- the V2 retarget rules ------------------------------------------
    from saltgrain import pow as powfn

    V2 = k.RETARGET_V2_HEIGHT
    assert powfn.k_max_for(V2 - 1) == powfn.K_MAX
    assert powfn.k_max_for(V2) == powfn.K_MAX_V2
    ok(f"puzzle cap rises from {powfn.K_MAX} to {powfn.K_MAX_V2} at height {V2}")

    # A target far past the cap is clamped back to the ceiling from V2 on,
    # and left alone below it. Difficulty above the cap buys no extra work.
    absurd = k.target_to_bits(k.bits_to_target(k.POW_LIMIT_BITS) // 10**20)
    assert k.next_bits(V2 - 1, absurd, 0, 0) == absurd
    capped = k.next_bits(V2, absurd, 0, 0)
    assert capped != absurd
    # nBits keeps three mantissa bytes, so the encoded target sits at or just
    # under the ceiling — never above it.
    ceiling = k.ceiling_target(V2)
    assert ceiling * 999 // 1000 <= k.bits_to_target(capped) <= ceiling
    assert powfn.k_for_work(k.target_to_work(k.bits_to_target(capped)), V2) == powfn.K_MAX_V2
    ok("difficulty above the puzzle cap is clamped to the ceiling from V2")

    # The retarget window. Sixteen timestamps span fifteen intervals, so on a
    # chain running exactly on target the pre-V2 window reads 15/16 of the
    # timespan and tightens difficulty by 6.67% every time. From V2 the window
    # starts one block earlier and a perfectly-paced chain holds steady.
    class _B:
        def __init__(self, h, ts, bits):
            self.height, self.timestamp, self.bits = h, ts, bits

    def window_bits(boundary):
        flat = [_B(h, h * k.TARGET_SPACING, k.GENESIS_BITS) for h in range(boundary)]
        return chainmod.bits_for_height(boundary, flat)

    genesis_target = k.bits_to_target(k.GENESIS_BITS)
    pre = window_bits(k.RETARGET_INTERVAL * 8)  # a boundary well below V2
    assert k.bits_to_target(pre) == genesis_target * 15 // 16
    post = window_bits(V2 + k.RETARGET_INTERVAL - (V2 % k.RETARGET_INTERVAL))
    assert k.bits_to_target(post) == genesis_target
    ok("pre-V2 window ran 6.67% hot; the V2 window holds an on-target chain flat")

    # ---- final state ---------------------------------------------------
    final = chainmod.replay(good, now=now)
    print()
    print(f"  {len(PASSED)} checks passed")
    print(f"  height {final.height}  tip {final.tip_hash[:24]}…")
    print(f"  chainwork {final.chainwork:,} expected hashes")
    print(f"  supply {k.format_amount(final.circulating())} SALT")
    return 0


if __name__ == "__main__":
    sys.exit(run())
