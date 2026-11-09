# SFT 合成数据生成器（gen_sft_synth.py）

从**衡策自己的回测引擎与行情接口**里产出可追溯的 SFT 轨迹，不引入任何外部问答对。
答案里的每个数字都能回到一条 `source_id`（回测结果或行情快照），source 落在 `sources.jsonl` 里。

## 1. 前提

| 项 | 要求 |
| --- | --- |
| 运行位置 | `backend/` 目录下（脚本自己把 `backend` 加进 `sys.path`） |
| Python | 3.10+（用到 `X \| Y` 类型注解） |
| 依赖 | `baostock`、`pandas`、`numpy`、`loguru`（均在 `requirements.txt` 里） |
| 网络 | 能登录 baostock（失败会走新浪 K 线兜底）；`get_quote` 还会访问东财快照 |
| LLM | **不需要**。脚本不调大模型，只用现有工具函数 |

```powershell
cd D:\code\0825code\wealthai\backend
python scripts\gen_sft_synth.py
```

缺依赖或网络不通时会直接抛错并说明卡在哪个标的/哪一步，不会静默产出空文件。

## 2. 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--symbols` | `600519,000001,300750,601318,000858,002594,600036,601899,300059,600030` | 逗号分隔 A 股代码 |
| `--strategies` | `ma_cross,rsi_ma200,donchian_breakout` | 必须存在于 `STRATEGY_META` |
| `--windows` | `60,120,250` | 回测请求天数（实际以 `bars_count` 为准） |
| `--capital` | `200000` | 回测初始资金 |
| `--qty` | `100` | 持仓样本股数 |
| `--cost-lookback` | `60` | 成本价取多少个交易日前的收盘 |
| `--seed` | `20261107` | 问题措辞与切分顺序 |
| `--out` | `backend/data/sft_synth` | 输出目录（新建，不覆盖 `data/finetune`） |
| `--no-negatives` | 关 | 跳过负样本 |
| `--max-samples` | 0 | >0 时截断 |

小样本试跑：

```powershell
python scripts\gen_sft_synth.py --symbols 600519,000001 --strategies ma_cross --windows 120 --no-negatives --out "$env:TEMP\sft_demo"
```

## 3. 产物

| 文件 | 内容 |
| --- | --- |
| `train.jsonl` / `val.jsonl` / `test.jsonl` | 训练样本，一行一条 |
| `sources.jsonl` | 每个 `source_id` 对应的原始结果快照（quote / backtest / hist_bar / risk_snapshot） |
| `provenance.jsonl` | sample_id → source_ids → 参数 → 登记数字 → 日期 → 所属 split |
| `reject.jsonl` | 被闸门剔除的样本（含 `answer_excerpt`），为空时是 0 字节 |
| `manifest.json` | 条数、意图分布、来源分布、剔除分类计数、泄漏自检、跳过原因 |

样本结构：

```json
{
  "sample_id": "sft-000006",
  "intent": "investment_analysis",
  "task": "backtest_review",
  "messages": [
    {"role": "system", "content": "你是衡策……不构成投资建议。"},
    {"role": "user", "content": "帮我验证「均线金叉死叉」在 sh.600519 上的表现，跑 120 个交易日。\n\n<data>…</data>"},
    {"role": "assistant", "content": "我调用回测引擎……"}
  ],
  "tool_trace": [{"step": 1, "tool": "run_strategy_backtest", "args": {}, "ok": true, "source_id": "bt:sh.600519:ma_cross:120:200000:39fbd7ce61"}],
  "provenance": {"source_ids": ["bt:…"], "params": {}, "numbers": [], "dates": [], "facts": {}, "interval_key": "sh.600519|2026-05-14|2026-11-06"}
}
```

- `messages` 可直接被 `/api/datasets/upload` 的 `upload_jsonl` 解析（它优先读 `messages`，其余字段忽略）。
- user 侧用 `<data>` 包裹工具结果，与线上 `node_analysis` 的注入方式一致，避免训练/推理不同构。

## 4. 覆盖范围

| 意图 | 是否生成 | 数据来源 |
| --- | --- | --- |
| `market_query` 看盘 | ✅ 每个标的 1 条 | `get_quote` + `compute_indicators` |
| `portfolio_query` 持仓盈亏 | ✅ 每个标的 1 条 | `get_historical`（买入日收盘）+ `get_quote` |
| `investment_analysis` 回测验证 | ✅ 标的 × 策略 × 窗口 | `run_strategy_backtest` |
| `risk_assessment` 风险提示 | ✅ 每个标的 1 条 | `compute_indicators`（volatility20/pos60）+ `node_risk` 分档 |
| `refusal` 负样本 | ✅ 每标的 2 条 + 6 条固定 | 真实报错 / 能力边界 / 预测拒答 |
| `chitchat` | ❌ | 无真实数据可锚定 |
| `alert_config` | ❌ | 纯配置动作，没有可回填结果 |
| `screener` | ❌ | 全市场扫描，单次成本高且不是单标的结论 |
| `knowledge_query` | ❌ | 依赖 RAG 语料，无数字可校验 |

负样本三分法：**数据不足**（`999999` 真实取数失败）、**能力边界**（美股走 A 股回测、截面策略、逐笔/十档、分钟级、机构持仓）、**预测涨跌/目标价**（拒绝但给出真实的 20 日涨跌幅与波动率）。统一写「无法完成 → 缺什么 → 能提供什么 → 下一步」，不写「我不确定」。

## 5. 质量闸门

| reason | 含义 |
| --- | --- |
| `schema` | 不是 system/user/assistant 三段或存在空内容 |
| `length_answer` | 答案 < 60 或 > 1500 字符 |
| `length_total` | 三段合计 > 4000 字符 |
| `refusal_as_normal` | 正常意图里出现拒答话术，或拒答样本缺少「缺什么/能提供什么」 |
| `provenance_missing` / `provenance_broken` | 正常样本无 source_id，或 source 未落盘 |
| `unregistered_number` | 答案里出现未登记数字/未登记日期（核心闸门） |
| `dup_question` / `dup_answer` | 问题级 / 答案级重复 |

数字闸门的实现：生成时所有数值都必须经 `FactBook.n()/c()` 登记，登记结果写入 `provenance.numbers`；校验时把答案里的数字（先掩掉日期与股票代码）逐个与登记集合比对，多一个就整条剔除。

额外规则：`trades_count == 0` 的回测（全 0.00%）不进数据集，记入 `manifest.skipped` 的 `no_trades:*`。

## 6. 切分与泄漏

按**标的分组**切 70/15/15：同一标的的全部样本只会落在一个 split。
`manifest.leakage_check` 会输出 `symbols_in_multiple_splits` 与 `intervals_in_multiple_splits`，两者都应为空；`interval_key` 记录每条的 `symbol|起|止`，用于人工复核「同标的同区间」不会跨集。

## 7. 实测（2026-11-07）

**默认参数**（10 标的 × 3 策略 × 3 窗口）：

- 生成 146，保留 **113**（train 69 / val 22 / test 22），reject 0，`no_trades` 跳过 33
- 行情源失败 0 个标的，耗时约 **35 秒**

**扩参**（40 标的 × 4 策略 × 5 窗口 `90,120,150,180,250`，40 个标的见下）：

```powershell
python scripts\gen_sft_synth.py `
  --symbols "600519,000001,300750,601318,000858,002594,600036,601899,300059,600030,601398,601288,600900,601088,600028,600887,000568,002304,600276,300760,002415,000333,000651,601888,600309,600585,601668,000002,601012,002475,300124,600050,601728,600941,601988,601166,000725,600690,601601,600048" `
  --strategies "ma_cross,macd,donchian_breakout,boll_reversion" `
  --windows "90,120,150,180,250"
```

- 候选 1006，其中 16 条 0 成交回测在生成前跳过，最终生成并保留 **990**（train 689 / val 146 / test 155），reject 0
- `no_trades` 跳过 16；行情源失败 0 个标的；泄漏自检 0
- 耗时约 **159 秒**
- 意图分布：回测 784 / 看盘 40 / 风险 40 / 持仓 40 / 拒答 86

## 8. 已知边界

1. **不是评测集**：样本由模板生成，只能做 SFT 冷启动；效果必须用独立留出集 + 人工抽检评估。
2. **`--windows 250` 拿不满**：引擎按日历日回溯取数，250 交易日实际约 240 条左右，正文一律用 `bars_count` 的真实值，不写请求值。
3. **策略名里的数字**（如 `RSI + MA200`）会作为常量登记，属正常，不是泄漏。
4. **行情接口限频**：默认 10 标的 35 秒；扩到几十个标的时会明显变慢，`fetch_history_bars` 自带缓存（同标的同窗口只取一次）。
5. **数据不可外传**：行情来自 baostock/东财/新浪，仅限本地训练与演示，不要提交到公开数据集或商用。
6. 生成结果默认写到 `backend/data/sft_synth/`，与 `data/finetune/`、已有标注数据互不影响。
