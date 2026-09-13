#!/usr/bin/env python3
"""
Independently verify the Saltgrain chain.

    python3 verify.py

Replays every block from genesis: recomputes each hash, re-derives every
difficulty retarget, rebuilds the merkle root, checks every signature and
every coinbase amount against the subsidy schedule.

Exits 0 if the chain is valid, 1 if it is not. Nothing here trusts the
README, the workflow, or any stored state -- only chain/blocks.jsonl.
"""

import sys

from saltgrain import chain as chainmod
from saltgrain.consensus import (
    CHAIN_NAME,
    COIN,
    ConsensusError,
    bits_to_target,
    difficulty,
    format_amount,
)


def main() -> int:
    try:
        blocks = chainmod.load_blocks()
    except ConsensusError as exc:
        print(f"FAIL  {exc}")
        return 1

    if not blocks:
        print("chain is empty -- no genesis block")
        return 1

    try:
        state = chainmod.replay(blocks)
    except ConsensusError as exc:
        print(f"FAIL  {exc}")
        return 1

    emitted = chainmod.emitted_supply(state.height)
    circulating = state.circulating()

    print(f"{CHAIN_NAME} chain verified")
    print(f"  height        {state.height}")
    print(f"  tip           {state.tip_hash}")
    print(f"  blocks        {len(blocks)}")
    print(f"  transactions  {state.tx_count}")
    print(f"  chainwork     {state.chainwork:,} expected hashes")
    print(f"  difficulty    {difficulty(state.tip.bits):,.1f}  (bits {state.tip.bits:#010x})")
    print(f"  next bits     {state.next_bits():#010x}")
    print(f"  emitted       {format_amount(emitted)} SALT")
    print(f"  unspent       {format_amount(circulating)} SALT across "
          f"{len(state.utxos.utxos)} outputs")

    if emitted != circulating:
        print(f"FAIL  supply mismatch: {emitted} emitted but {circulating} unspent")
        return 1

    print()
    print("  miners")
    for handle, count in sorted(state.miners.items(), key=lambda p: (-p[1], p[0])):
        print(f"    {handle:<24} {count:>4} block(s)")

    print()
    print("  all hashes, targets, merkle roots, signatures and subsidies check out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
