"""
Chain state: loading, replaying and extending the Saltgrain block chain.

The canonical chain lives in chain/blocks.jsonl, one JSON block per line,
in height order. Everything else -- balances, the UTXO set, difficulty --
is derived by replaying that file from genesis, so there is no state to
corrupt and nothing to trust.
"""

import json
import os
import time

from .consensus import (
    Block,
    ConsensusError,
    RETARGET_INTERVAL,
    UTXOSet,
    bits_to_target,
    block_subsidy,
    median_time_past,
    next_bits,
    target_to_work,
    validate_block,
)

BLOCKS_FILE = os.path.join("chain", "blocks.jsonl")   # 旧布局：单个大文件
BLOCK_DIR = os.path.join("chain", "blocks")           # 新布局：按高度分片
INDEX_FILE = os.path.join("chain", "index.json")
SHARD_SIZE = 50                                        # 每片 50 块 ≈ 750KB，压在 contents API 的 1MB 之下


def shard_name(start: int) -> str:
    return f"{start:06d}-{start + SHARD_SIZE - 1:06d}.jsonl"


def load_shards():
    """按顺序读出所有分片；目录不存在就返回 None，让调用方回退到单文件。"""
    if not os.path.isdir(BLOCK_DIR):
        return None
    files = sorted(f for f in os.listdir(BLOCK_DIR) if f.endswith(".jsonl"))
    blocks = []
    for name in files:
        with open(os.path.join(BLOCK_DIR, name), "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    blocks.append(Block.from_dict(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    raise ConsensusError(f"{name}:{lineno}: malformed block ({exc})")
    return blocks


def write_index(blocks) -> None:
    """给浏览器和采盐机看的清单：有哪些分片、链尖在哪。"""
    files = sorted(f for f in os.listdir(BLOCK_DIR) if f.endswith(".jsonl"))
    index = {
        "shard_size": SHARD_SIZE,
        "shards": [f for f in files],
        "height": len(blocks) - 1 if blocks else -1,
        "tip": blocks[-1].block_hash() if blocks else "00" * 32,
    }
    with open(INDEX_FILE, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"), sort_keys=True)


def load_blocks(path: str = BLOCKS_FILE):
    shards = load_shards()
    if shards is not None:
        return shards
    if not os.path.exists(path):
        return []
    blocks = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                blocks.append(Block.from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                raise ConsensusError(f"{path}:{lineno}: malformed block ({exc})")
    return blocks


def append_block(block: Block, path: str = BLOCKS_FILE) -> None:
    """把块写进它所属的分片。分片目录存在就只碰那一个文件，不再重写整条账本。"""
    if os.path.isdir(BLOCK_DIR):
        os.makedirs(BLOCK_DIR, exist_ok=True)
        start = (block.height // SHARD_SIZE) * SHARD_SIZE
        with open(os.path.join(BLOCK_DIR, shard_name(start)), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(block.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")
        write_index(load_shards() or [])
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(block.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")


def bits_for_height(height: int, blocks) -> int:
    """
    The difficulty a block at `height` must use.

    Block 0 uses the genesis difficulty. On a retarget boundary the window is
    the previous RETARGET_INTERVAL blocks.

    From RETARGET_V2_HEIGHT the window starts one block earlier, so it spans
    16 real intervals rather than 15. Measuring 15 against a 16-interval
    target biased every retarget by 6.67% -- the same off-by-one Bitcoin has
    carried since 2009, and which this chain's spec wrongly claimed to avoid.
    """
    from .consensus import GENESIS_BITS, RETARGET_V2_HEIGHT

    if height == 0:
        return GENESIS_BITS
    prev = blocks[height - 1]

    if height % RETARGET_INTERVAL != 0:
        return next_bits(height, prev.bits, 0, 0)

    offset = RETARGET_INTERVAL + 1 if height >= RETARGET_V2_HEIGHT else RETARGET_INTERVAL
    first_index = height - offset
    if first_index < 0:
        return next_bits(height, prev.bits, 0, 0)
    return next_bits(height, prev.bits, blocks[first_index].timestamp, prev.timestamp)


class ChainState:
    """The result of a full replay: UTXO set, chainwork, per-miner tallies."""

    def __init__(self):
        self.blocks = []
        self.utxos = UTXOSet()
        self.chainwork = 0
        self.miners = {}  # handle -> blocks mined
        self.tx_count = 0

    @property
    def height(self) -> int:
        return len(self.blocks) - 1

    @property
    def tip(self):
        return self.blocks[-1] if self.blocks else None

    @property
    def tip_hash(self) -> str:
        return self.tip.block_hash() if self.tip else "00" * 32

    def next_bits(self) -> int:
        return bits_for_height(len(self.blocks), self.blocks)

    def median_time_past(self) -> int:
        return median_time_past(self.blocks)

    def circulating(self) -> int:
        return self.utxos.total()

    def adopt(self, block: Block, utxos: "UTXOSet") -> None:
        """
        Record a block that has already passed validate_block, without
        replaying the chain again.

        `utxos` must be the set validate_block returned for this block. This
        exists so the node that just validated-and-appended a submission can
        render the new state without a second full replay, which is the most
        expensive thing in the whole submission path.
        """
        self.blocks.append(block)
        self.utxos = utxos
        self.chainwork += target_to_work(bits_to_target(block.bits))
        self.miners[block.miner] = self.miners.get(block.miner, 0) + 1
        self.tx_count += len(block.txs)


def replay(blocks, now: int | None = None, strict_time: bool = True) -> ChainState:
    """
    Validate every block from genesis and return the resulting state.

    Raises ConsensusError naming the first block that breaks a rule.
    """
    if now is None:
        now = int(time.time())
    state = ChainState()

    for i, block in enumerate(blocks):
        prev = state.blocks[-1] if state.blocks else None
        expected = bits_for_height(i, state.blocks)
        mtp = median_time_past(state.blocks)
        check_now = now if strict_time else block.timestamp
        try:
            new_utxos = validate_block(block, prev, state.utxos, expected, mtp, check_now)
        except ConsensusError as exc:
            raise ConsensusError(f"block {i} ({block.block_hash()[:16]}…): {exc}") from None
        state.adopt(block, new_utxos)

    return state


def load_state(path: str = BLOCKS_FILE, now: int | None = None) -> ChainState:
    return replay(load_blocks(path), now=now)


def emitted_supply(height: int) -> int:
    return sum(block_subsidy(h) for h in range(height + 1))
