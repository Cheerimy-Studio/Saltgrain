#!/usr/bin/env python3
"""
Process one submission from a GitHub issue comment.

Called by .github/workflows/. Reads the comment body and the comment
author, validates whatever it finds, updates the chain or the mempool, and
writes a reply to stdout (and to $GITHUB_OUTPUT as `reply`).

Exit code is always 0: a rejected submission is a normal outcome that
deserves an explanatory reply, not a red X on the workflow.
"""

import base64
import json
import os
import sys
import time
import urllib.request

from saltgrain import chain as chainmod
from saltgrain import crypto
from saltgrain import render
from saltgrain.consensus import (
    Block,
    ConsensusError,
    Tx,
    UTXOSet,
    bits_to_target,
    difficulty,
    format_amount,
    validate_block,
    validate_tx,
)

BLOCK_PREFIX = "salt-block-v1:"
TX_PREFIX = "salt-tx-v1:"
ID_PREFIX = "salt-id-v1:"
MEMPOOL = os.path.join("chain", "mempool.jsonl")
REGISTRY = os.path.join("chain", "registry.json")
MAX_MEMPOOL = 64

# Mining is gated on having starred the repository. Set SALT_REQUIRE_STAR=0 in
# the environment to lift the gate (useful when testing the node locally).
REQUIRE_STAR = os.environ.get("SALT_REQUIRE_STAR", "1") != "0"


def emit(reply: str, changed: bool) -> None:
    print(reply)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            delim = "SALT_EOF_%d" % int(time.time())
            fh.write(f"reply<<{delim}\n{reply}\n{delim}\n")
            fh.write(f"changed={'true' if changed else 'false'}\n")


def find_payload(body: str):
    for line in body.splitlines():
        line = line.strip().strip("`")
        if line.startswith(BLOCK_PREFIX):
            return "block", line[len(BLOCK_PREFIX):].strip()
        if line.startswith(TX_PREFIX):
            return "tx", line[len(TX_PREFIX):].strip()
        if line.startswith(ID_PREFIX):
            return "id", line[len(ID_PREFIX):].strip()
    return None, None


def decode(payload: str):
    raw = base64.b64decode(payload, validate=True)
    if len(raw) > 64_000:
        raise ValueError("submission too large")
    return json.loads(raw)


def author_has_starred(author: str) -> bool:
    """
    True if `author` has starred this repository.

    Mining is the only submission type behind this gate. It is the one thing
    the node can check about a miner that costs nothing, supports the project,
    and is trivial to prove from the API.

    Outside Actions -- no repository or token in the environment -- this
    returns True, so local runs and the test suite are never blocked by a
    check they cannot perform. A GitHub API failure also returns True: a
    network blip must not throw away a block that took real work to mine.
    """
    if not REQUIRE_STAR:
        return True
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        return True
    page = 1
    while page <= 20:  # 20,000 stargazers; far past anything this chain needs
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/stargazers"
            f"?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "saltgrain-node",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                users = json.load(response)
        except Exception:  # noqa: BLE001 - any API problem fails open
            return True
        if not users:
            return False
        if any(str(u.get("login", "")).lower() == author.lower() for u in users):
            return True
        if len(users) < 100:
            return False
        page += 1
    return True


def load_mempool():
    if not os.path.exists(MEMPOOL):
        return []
    with open(MEMPOOL, encoding="utf-8") as fh:
        return [Tx.from_dict(json.loads(l)) for l in fh if l.strip()]


def save_mempool(txs) -> None:
    os.makedirs(os.path.dirname(MEMPOOL), exist_ok=True)
    with open(MEMPOOL, "w", encoding="utf-8") as fh:
        for t in txs:
            fh.write(json.dumps(t.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")


def handle_identity(payload: str, author: str) -> tuple[str, bool]:
    """
    Bind a GitHub handle to a Saltgrain address.

    Two proofs are required and neither alone is enough: the signature shows
    control of the key, and posting from the account shows control of the
    handle. This is not consensus -- it only decides whose name is shown
    beside a balance, and verify.py ignores it entirely.
    """
    parts = payload.split(":")
    if len(parts) != 3:
        return "**已拒绝。** 名字绑定那一行格式不对。请运行 `python3 wallet.py identity`。", False
    handle, pubkey, sig = parts

    if handle.lower() != author.lower():
        return (
            f"**已拒绝。** 那一行声称属于 `{handle}`，但这条评论是 @{author} 发的。"
            f"请用 `--handle {author}` 重新运行。",
            False,
        )
    try:
        pub_bytes = bytes.fromhex(pubkey)
        sig_bytes = bytes.fromhex(sig)
        address = crypto.pubkey_to_address(pub_bytes)
    except ValueError:
        return "**已拒绝。** 公钥或签名格式不对。", False

    digest = crypto.sha256d(b"saltgrain-identity-v1|" + handle.encode())
    if not crypto.verify(pub_bytes, digest, sig_bytes):
        return "**已拒绝。** 这个签名对该用户名验证不通过。", False

    registry = {}
    if os.path.exists(REGISTRY):
        with open(REGISTRY, encoding="utf-8") as fh:
            registry = json.load(fh)
    previous = registry.get(handle, {}).get("address")
    registry[handle] = {"address": address, "pubkey": pubkey}
    with open(REGISTRY, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)

    state = chainmod.load_state()
    render.update_readme(state)

    note = f"\n\n这取代了你之前的地址 `{previous}`。" if previous else ""
    return (
        f"### 名字已绑定\n\n"
        f"| | |\n|---|---|\n"
        f"| GitHub | @{handle} |\n| 地址 | `{address}` |\n\n"
        f"从现在起，账本里你的余额旁边会显示你的名字。{note}",
        True,
    )


def handle_block(data, author: str) -> tuple[str, bool]:
    block = Block.from_dict(data)

    if block.miner.lower() != author.lower():
        return (
            f"**已拒绝。** 这个区块署名矿工是 `{block.miner}`，但提交者是 @{author}。"
            f"矿工名字被写进了区块头，改动它就得把工作量全部重做。\n\n"
            f"请用 `--miner {author}` 重新挖。",
            False,
        )

    if not author_has_starred(author):
        return (
            f"**已拒绝。** 挖矿前请先给本仓库点一个 Star（右上角 ★ Star）。\n\n"
            f"@ {author} 还没有 Star 过这里。点完之后，把这条评论原样再发一次即可 —— "
            f"区块本身仍然有效（除非这期间别人先挖出了下一个区块）。\n\n"
            f"> 转账和名字绑定不受此限制，随时可以提交。",
            False,
        )

    blocks = chainmod.load_blocks()
    state = chainmod.replay(blocks)
    expected_bits = state.next_bits()

    try:
        new_utxos = validate_block(
            block,
            state.tip,
            state.utxos,
            expected_bits,
            state.median_time_past(),
            int(time.time()),
        )
    except ConsensusError as exc:
        return (
            f"**已拒绝。** {exc}\n\n"
            f"当前链尖是 `{state.tip_hash}`（高度 `{state.height}`），"
            f"下一个区块需要 bits `{expected_bits:#010x}`。",
            False,
        )

    chainmod.append_block(block)

    included = {t.txid() for t in block.txs[1:]}
    if included:
        save_mempool([t for t in load_mempool() if t.txid() not in included])

    # validate_block already computed the resulting UTXO set, so there is no
    # reason to replay the whole chain a second time just to render it.
    new_state = state
    new_state.adopt(block, new_utxos)
    render.update_readme(new_state)
    render.render_svg(new_state)

    reward = format_amount(sum(o.value for o in block.txs[0].outputs))
    msg = block.txs[0].coinbase
    lines = [
        f"### 区块 `{block.height}` 已接受",
        "",
        f"| | |",
        f"|---|---|",
        f"| 哈希 | `{block.block_hash()}` |",
        f"| 矿工 | @{block.miner} |",
        f"| 解出的题数 | `{block.puzzles()}` |",
        f"| 难度 | `{difficulty(block.bits):,.1f}` |",
        f"| 奖励 | `{reward} SALT` |",
        f"| 交易数 | `{len(block.txs)}` |",
    ]
    if msg:
        lines.append(f"| 留言 | `{msg}` |")
    lines += [
        "",
        f"链现在的高度是 `{new_state.height}`，下一块难度 "
        f"`{difficulty(new_state.next_bits()):,.1f}`。",
        "",
        f"README 已更新。你可以用 `python3 verify.py` 独立核验整条链。",
    ]
    return "\n".join(lines), True


def handle_tx(data, author: str) -> tuple[str, bool]:
    tx = Tx.from_dict(data)
    if tx.is_coinbase:
        return "**已拒绝。** 区块奖励由矿工创建，不能直接提交。", False

    state = chainmod.load_state()
    mempool = load_mempool()

    if len(mempool) >= MAX_MEMPOOL:
        return f"**已拒绝。** 交易池满了（{MAX_MEMPOOL} 笔）。先挖一个区块清一清。", False
    if any(t.txid() == tx.txid() for t in mempool):
        return f"这笔交易已经在池子里了：`{tx.txid()[:20]}…`。", False

    # Validate against the chain tip plus everything already queued, so two
    # queued transactions cannot spend the same output.
    working: UTXOSet = state.utxos.copy()
    height = state.height + 1
    for t in mempool:
        try:
            validate_tx(t, working, height)
        except ConsensusError:
            continue
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)

    try:
        fee = validate_tx(tx, working, height)
    except ConsensusError as exc:
        return f"**已拒绝。** {exc}", False

    mempool.append(tx)
    save_mempool(mempool)

    total = sum(o.value for o in tx.outputs)
    return (
        "\n".join(
            [
                f"### 交易已排队",
                "",
                f"| | |",
                f"|---|---|",
                f"| txid | `{tx.txid()}` |",
                f"| 转出 | `{format_amount(total)} SALT`，共 {len(tx.outputs)} 个输出 |",
                f"| 手续费 | `{format_amount(fee)} SALT` |",
                f"| 交易池 | `{len(mempool)}` 笔待打包 |",
                "",
                "挖出下一个区块的人会把它打包进去，手续费高的优先。",
            ]
        ),
        True,
    )


def main() -> int:
    body = os.environ.get("COMMENT_BODY", "")
    author = os.environ.get("COMMENT_AUTHOR", "")

    if not author:
        emit("**已拒绝。** 没拿到评论作者。", False)
        return 0

    kind, payload = find_payload(body)
    if not kind:
        emit(
            "这条评论里没有我能识别的内容。\n\n"
            f"区块以 `{BLOCK_PREFIX}` 开头，转账以 `{TX_PREFIX}` 开头，"
            f"名字绑定以 `{ID_PREFIX}` 开头，各自单独一行。"
            "生成方法见 README。",
            False,
        )
        return 0

    if kind == "id":
        try:
            reply, changed = handle_identity(payload, author)
        except Exception as exc:  # noqa: BLE001
            reply, changed = f"**已拒绝。** `{type(exc).__name__}: {exc}`", False
        emit(reply, changed)
        return 0

    try:
        data = decode(payload)
    except Exception as exc:  # noqa: BLE001 - any malformed paste lands here
        emit(f"**已拒绝。** 无法解码这个 {kind}：`{exc}`", False)
        return 0

    try:
        reply, changed = handle_block(data, author) if kind == "block" else handle_tx(data, author)
    except ConsensusError as exc:
        reply, changed = f"**已拒绝。** {exc}", False
    except Exception as exc:  # noqa: BLE001
        reply, changed = f"**已拒绝。** {kind} 格式有问题：`{type(exc).__name__}: {exc}`", False

    emit(reply, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
