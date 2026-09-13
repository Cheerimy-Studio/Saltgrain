#!/usr/bin/env python3
"""
Keeps the chain moving when nothing else is.

Runs on a schedule. If the tip is younger than the target spacing it exits
without doing anything, so an active chain never sees it. If the tip has gone
stale it mines one block at the current difficulty, which pins block spacing
near the target during quiet stretches and lets real miners displace it the
moment they show up.

The reward goes to the address that holds the genesis output, read from the
chain itself, so there is nothing to configure and no key involved: mining
needs only the public address.

Coinbase messages are drawn from satoshinotebook.com, an open-source notebook
on bitcoin and first principles by @satoshinotebook, one line at a time.
"""

import html as htmllib
import os
import random
import re
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor

from saltgrain import chain as chainmod
from saltgrain import pow as powfn
from saltgrain import render
from saltgrain.consensus import (
    MAX_MESSAGE_BYTES,
    TARGET_SPACING,
    Block,
    ConsensusError,
    Tx,
    TxOut,
    block_subsidy,
    check_message,
    difficulty,
    format_amount,
    merkle_root,
    validate_tx,
)

MINER = os.environ.get("SALT_MINER", "Cheerimy-Studio")
STALE_AFTER = int(os.environ.get("SALT_STALE_AFTER", TARGET_SPACING))

QUOTE_HOST = "https://satoshinotebook.com"
QUOTE_PAGES = [
    "/reality", "/scarcity", "/the-language-of-scarcity", "/the-arrow-of-time",
    "/money-is-interopability", "/money-is-energy", "/properties-of-money",
    "/the-universal-coincidence", "/legacy-money", "/flaws-of-modern-finance",
    "/inflation-is-a-problem", "/trust-is-a-problem", "/broken-money", "/decay",
    "/perfect-storage", "/properties-of-truth", "/the-discovery",
    "/bitcoin-is-the-first-engineered-money", "/bitcoin-is-mathematical-truth",
    "/bitcoin-is-sound-money", "/bitcoin-is-indestructible", "/bitcoin-is-scarce",
    "/bitcoin-is-impartial", "/bitcoin-is-immutable", "/bitcoin-is-verifiable",
    "/bitcoin-is-pure",
]

# Used only when the site cannot be reached.
FALLBACK = [
    "nature reveals itself through limitation",
    "value exists because we cannot have everything",
    "a prime number is prime for everyone",
    "energy cannot be created or destroyed",
    "our time is finite and irreversible",
    "it succeeds not because it is useful, but because it is true",
]

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")

# A chapter can wrap one sentence across several lines. Anything opening with
# one of these is the tail of a thought, not a whole one.
CONTINUATION = {
    "of", "and", "but", "or", "to", "in", "into", "for", "with", "that",
    "which", "from", "as", "at", "by", "on", "than", "then", "so", "yet",
    "because", "while", "where", "when", "through", "without", "against",
}


def quote() -> str:
    """One line, short enough for a coinbase, from a random chapter."""
    pages = random.sample(QUOTE_PAGES, k=min(3, len(QUOTE_PAGES)))
    for path in pages:
        try:
            req = urllib.request.Request(
                QUOTE_HOST + path, headers={"User-Agent": "saltgrain-chain/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - any network trouble falls through
            continue

        body = html.split("satoshi_notebook")[0]
        body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body)
        text = htmllib.unescape(TAG.sub("\n", body))
        picks = []
        for line in re.split(r"\n|(?<=\.)\s+", text):
            line = WS.sub(" ", line).strip(" •-•").strip().rstrip(".")
            if not line or line[0].isupper():
                continue
            if "|" in line or ":" in line or '"' in line:
                continue
            if any(ord(c) < 32 for c in line):
                continue
            words = line.split()
            if len(words) < 5:
                continue
            if words[0].lower() in CONTINUATION:
                continue
            if not 34 <= len(line.encode()) <= MAX_MESSAGE_BYTES:
                continue
            picks.append(line)
        if picks:
            return random.choice(picks)
    return random.choice(FALLBACK)


def solve(args):
    core, j = args
    return powfn.solve_puzzle(core, j)


def main() -> int:
    state = chainmod.load_state()
    age = int(time.time()) - state.tip.timestamp

    if age < STALE_AFTER:
        print(f"tip is {age}s old, under the {STALE_AFTER}s threshold — nothing to do")
        return 0

    height = state.height + 1
    bits = state.next_bits()
    payout = state.blocks[0].txs[0].outputs[0].address

    message = quote()
    try:
        check_message(message)
    except ConsensusError:
        message = random.choice(FALLBACK)

    mempool = []
    path = os.path.join("chain", "mempool.jsonl")
    if os.path.exists(path):
        import json

        with open(path, encoding="utf-8") as fh:
            mempool = [Tx.from_dict(json.loads(l)) for l in fh if l.strip()]

    chosen, fees = [], 0
    working = state.utxos.copy()
    for tx in mempool:
        try:
            fee = validate_tx(tx, working, height)
        except ConsensusError:
            continue
        for i in tx.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(tx, height)
        chosen.append(tx)
        fees += fee

    coinbase = Tx(
        coinbase=message,
        cb_height=height,
        outputs=[TxOut(block_subsidy(height) + fees, payout)],
    )
    txs = [coinbase] + chosen

    block = Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=merkle_root([t.txid() for t in txs]),
        timestamp=max(int(time.time()), state.median_time_past() + 1),
        bits=bits,
        miner=MINER,
        txs=txs,
    )

    k = block.puzzles()
    print(f"tip is {age}s old — mining height {height}")
    print(f"  difficulty {difficulty(bits):,.1f}, {k} puzzle(s), {len(chosen)} tx from mempool")
    print(f'  message   "{message}"')

    core = block.header_core()
    started = time.time()
    with ProcessPoolExecutor() as pool:
        solutions = list(pool.map(solve, [(core, j) for j in range(k)], chunksize=1))
    block.solution = powfn.encode_solutions(solutions)
    print(f"  solved in {time.time() - started:.0f}s")

    chainmod.append_block(block)

    fresh = chainmod.load_state()
    if fresh.tip_hash != block.block_hash():
        print("chain moved underneath us; leaving it alone", file=sys.stderr)
        return 1

    if chosen:
        included = {t.txid() for t in chosen}
        with open(path, "w", encoding="utf-8") as fh:
            import json

            for t in mempool:
                if t.txid() not in included:
                    fh.write(json.dumps(t.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")

    render.update_readme(fresh)
    render.render_svg(fresh)

    print(f"  height {fresh.height}  {block.block_hash()}")
    print(f"  reward {format_amount(block_subsidy(height) + fees)} SALT")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("mined=true\n")
            fh.write(f"height={fresh.height}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
