# 从零开一锅自己的盐

如果你想要一份**属于自己**的盐粒账本（而不是在别人的链上采），照着做。顺序别乱：创世块一旦推上去就改不了了，推之前是唯一能决定这条链长什么样的机会。

## 0. 把项目拿下来

```bash
git clone https://github.com/Cheerimy-Studio/Saltgrain.git 我的盐
cd 我的盐
rm chain/blocks.jsonl   # 那是别人的账本，你要自己开一锅
```

## 1. 建盐框

```bash
python3 saltbox.py new
```

会写出 `saltbox.json`（已在 `.gitignore` 里，但里面是明文私钥，别提交）。记下打印出的地址。

## 2. 采创世块

```bash
python3 make_genesis.py \
  --miner 你的GitHub用户名 \
  --message "创世留言，最多 80 字节" \
  --address salt1...你的地址...
```

大约半分钟。它会写出 `chain/blocks.jsonl`，渲染 README，并生成 `assets/ledger-*.svg`。

**创世留言是永久的。** 想一句你愿意被人反复念的话再写。

检查一下：

```bash
python3 verify.py
```

## 3. 建仓库并推上去

```bash
gh repo create 你的用户名/你的仓库名 --public \
  --description "一锅自己的盐"
git init && git add . && git commit -m "saltgrain: genesis"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

## 4. 开两个提交处

Fork 出来的仓库默认**关闭了 Issues**，先在 Settings 里打开，然后建标签和两个 issue：

```bash
gh label create salt --description "盐粒链提交" --color 8A5F10

gh issue create --title "采盐提交处 · Mine a block" --label salt --body \
"把 \`salt-block-v1:\` 那一行贴在这里。提交前需要先给仓库点 Star。"

gh issue create --title "寄送提交处 · Mempool" --label salt --body \
"把 \`salt-tx-v1:\` 那一行贴在这里排队。"
```

这两个 issue 最好落在 **#1**（采盐）和 **#2**（寄送），README 顶部的链接指向它们，`chain/issues.json` 里也记着这两个编号。如果编号不是 1 和 2，改这两处即可。

建好后把两个 issue 置顶（Pin），方便别人找到。

## 5. 给 Actions 写权限

Settings → Actions → General → Workflow permissions → 选 **Read and write permissions**。

不开这个，节点能验证但不能提交，所有提交都会卡在 push 那一步。Fork 的仓库还需要在 Actions 页面手动 Enable workflows 一次。

## 6. 打开盐仓

Settings → Pages → Source 选 **Deploy from a branch**，分支 `main`，目录 **`/docs`**。一分钟后盐仓就在 `https://你的用户名.github.io/你的仓库名/` 上线了。

如果仓库名和这里不一样，改 `docs/index.html` 里的一个属性：

```html
<html lang="zh-CN" data-repo="你的用户名/你的仓库名">
```

## 然后就可以采第一粒了

```bash
python3 miner.py --miner 你的用户名 --message "第一粒盐"
```

把输出贴到采盐提交处，看着 README 自己更新。

---

## 想调参数

常量集中在 `saltgrain/consensus.py` 顶部：

| 常量 | 作用 |
|---|---|
| `GENESIS_BITS` | 起始难度，`0x1e010000` 大约半分钟一块 |
| `TARGET_SPACING` | 期望的出块间隔（秒） |
| `RETARGET_INTERVAL` | 多少块调整一次难度 |
| `HALVING_INTERVAL` | 多少块奖励减半一次 |
| `COINBASE_MATURITY` | 奖励要等多少块才能花 |
| `MAX_TXS_PER_BLOCK` | 每个盐块最多打包多少笔交易 |

`saltgrain/pow.py` 里还有两个：

| 常量 | 作用 |
|---|---|
| `N` | 一道题的数字个数，越大越吃内存 |
| `K_MAX` / `K_MAX_V2` | 一个盐块最多解多少道题，决定盐块大小上限 |

**这些都要在创世之前改。** 创世之后再改，已有的链就作废了，`verify.py` 会直接告诉你。
