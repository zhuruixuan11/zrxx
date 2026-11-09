# backend/training — Qwen3.5 QLoRA 微调（SFT → DPO → 对比评测）

## 最短路径（开机即跑）

### 0. 前置（只做一次）

1. **镜像**：选 **PyTorch 2.5.1 / Python 3.12 / CUDA 12.4** 一类自带可用 torch 的镜像即可，
   `setup_gpu_env.sh` 会先探测再决定装什么，**不会覆盖镜像的 CUDA/torch**（见 §4.1「镜像自带 torch 时怎么装」）。
   若镜像 Python < 3.11，用 `PY=/path/to/python3.11 bash backend/training/setup_gpu_env.sh`。
2. **传代码**：`git clone`，命令见 §12.5。
3. **传数据**：按 §12.3 的 scp 命令传到 `/root/autodl-tmp/wealthai/backend/data/...`，传完按 §12.4 用 `wc -l` 验条数。
   - 训练与评测的 7 个 jsonl（`sft_synth`、`dpo_synth`、`eval_ood`）以 §12.3 为准；
   - 若要在机器上现场重跑 `prepare_data.py --mix`，再传 3 个输入源：
     `backend/data/sft_v3/final80/samples.jsonl`、`backend/data/sft_synth_v2/{train,val}.jsonl`。

### 1. 一条命令

```bash
cd /root/autodl-tmp/wealthai && bash backend/training/run_rented_gpu.sh
```

顺序：preflight（471/12/88 + GPU 自检）→ 建环境（没有 `.venv` 时）→
`prepare_data.py --mix`（幂等）→ SFT（读 yaml：8 epoch / 早停 / best ckpt）→
DPO（接 SFT 的 best adapter）→ `eval_compare.py` 在 88 条 OOD 上做 base / SFT / SFT+DPO 三档对比。

可覆盖变量：`MODEL=Qwen/Qwen3.5-4B OUT=backend/outputs BATCH=2 ACCUM=8 MAXLEN=1024
RESUME=auto DRYRUN=0 HF_ENDPOINT=https://hf-mirror.com`。

**断点续训**：`RESUME=auto`（默认）自动找 `$OUT/qwen3_5_4b_sft/checkpoint-*` 里 step 最大的传给
`train_sft.py --resume-from-checkpoint`；`RESUME=0` 从头训；`RESUME=/path/to/checkpoint-120` 指定某个 checkpoint。
checkpoint 只在每个 epoch 边界保存，续训最多回退一个 epoch 的进度。

**只验脚本、不训练**：`DRYRUN=1 bash backend/training/run_rented_gpu.sh`（只跑 preflight + 数据准备，本机就能验证）。

### 2. 预计耗时（估算，非实测）

3090 24G，471 行，有效批 16（30 步/epoch），按 4–6 s/step 估：

- SFT：每 epoch 2–3 min，8 epoch 约 **16–24 min**；早停若在第 5 轮触发约 **12–15 min**
- DPO：约 **5–8 min**
- OOD 88 条 × 3 档现场生成：约 **6–10 min**
- 拉模型：约 8GB（视带宽）
- **整体约 35–50 min**（首次建 venv / 装依赖另加 8–15 min，镜像已带依赖可忽略）

真实每步耗时以 Trainer 日志里的 `it/s` 和脚本最后打印的「步骤 / 耗时 / 峰值显存」表为准。

### 3. 峰值显存（估算，非实测）

| 阶段 | 峰值显存 | 24G 余量 |
| --- | --- | --- |
| SFT（QLoRA NF4，max_len 1024，batch 2×8） | 约 13–15GB | 充裕 |
| DPO（策略 + 参考共享同一基座） | 约 15–17GB | 约 7GB |

两者都按 3090 24G 估。OOM 时先 `BATCH=1 ACCUM=16`，仍不行再 `MAXLEN=768`（见下方失败点 4）。

### 4. 产物清单与拷回建议

`$OUT` 默认 `backend/outputs`。

| 产物 | 内容 | 是否必须拷回 |
| --- | --- | --- |
| `$OUT/eval_result.json` | 每个模型 × 每个指标的 numerator / denominator / rate | **必须** |
| `$OUT/eval_detail.csv` | 88 条逐条明细（UTF-8 BOM，Excel 可直接开） | **必须** |
| `$OUT/preds/{base,sft,dpo}.jsonl` | 三档现场生成的原始回答 | **必须** |
| `$OUT/qwen3_5_4b_sft/train_summary.json` | best_checkpoint / best_eval_loss / peak_vram_gb / 步数 | **必须** |
| `$OUT/qwen3_5_4b_sft/{best checkpoint, adapter}` | SFT 适配器，几 GB | 可不拷（要复现再训才需要） |
| `$OUT/qwen3_5_4b_dpo/` | DPO 适配器 + `train_summary.json`，几 GB | 适配器可不拷；`train_summary.json` 建议拷 |
| `$OUT/logs/*.log` | 每步完整日志 + `summary.tsv` | **必须** |

只拷必须项时，连同 `$OUT/logs/` 一起打包即可（体积 < 10MB）；adapter 几个 GB，除非要部署，否则不必下载。
关机前先把这些文件拉回本地：关机后 GPU 不预留，下次未必抢得到同型号。

## 失败点清单（5 条）

### 1. 数据文件缺失 / 行数不对

```bash
wc -l backend/data/training/mixed/train.jsonl    # 应 471
wc -l backend/data/training/core/val.jsonl       # 应 12
```

**修法**：跑 `python backend/training/prepare_data.py --mix` 现场重建；缺输入源（`sft_v3/final80/samples.jsonl`、
`sft_synth_v2/{train,val}.jsonl`）就按 §12.3 重传。脚本 preflight 会在开训前拦下，不会带着错数据往下跑。

### 2. 依赖版本漂移（transformers 5.17 移除了 `warmup_ratio`，TRL / peft 不匹配）

```bash
pip show transformers peft trl bitsandbytes | grep -E "Name|Version"
```

**修法**：重跑 `bash backend/training/setup_gpu_env.sh`，让它整组对齐
transformers 5.17.0 / peft 0.21.2 / trl 1.17.1 / bitsandbytes 0.50.2。别只升级其中一个包。

### 3. bitsandbytes 与 CUDA 不匹配（`CUDA Setup failed` / `No GPU found`）

```bash
python -c "import torch,bitsandbytes as b;print(torch.version.cuda,b.__version__)"
```

**修法**：按 cu126 重装 bnb：`pip install --force-reinstall bitsandbytes==0.50.2`；
实在不行改用 `--no-4bit` 走 bf16（`train_sft.py` / `train_dpo.py` 已支持，3090 24G 可容 4B bf16）。

### 4. 显存不足 OOM（日志出现 `CUDA out of memory`）

```bash
grep -n "CUDA out of memory" backend/outputs/logs/02_sft.log backend/outputs/logs/03_dpo.log
```

**修法**：`BATCH=1 ACCUM=16 bash backend/training/run_rented_gpu.sh`（有效批仍 16）；
仍 OOM 再 `MAXLEN=768`。降级顺序见 §7。

### 5. OOD 文件缺失或条数不对

```bash
wc -l backend/data/eval_ood/ood.jsonl            # 应 88
```

**修法**：按 §12.3 重新 scp `ood.jsonl`，不要用旧版 80 条；条数不对就是传断了。

---

覆盖「990 条 SFT → 270 对 DPO → eval_ood 独立集三方对比」的完整链路。
**先说结论：本机跑不了，必须租卡。** 下面是实测环境、显存对照、租卡方案和可直接执行的命令。

---

## 数据切分与配比

数据源与产物：

- 精写集 `backend/data/sft_v3/final80/samples.jsonl`（80 条，已封版，只读）
- 辅助集 `backend/data/sft_synth_v2/{train,val,test}.jsonl`（854 / 180 / 198）
- OOD `backend/data/eval_ood/ood.jsonl`（88 条，**只做最终评测，不参与训练、调参、选 checkpoint**）
- 切分产物 `backend/data/training/core/{train,val}.jsonl`（68 / 12）
- 混合训练集 `backend/data/training/mixed/train.jsonl`（471 行）+ `stats.json`
- 混合集唯一内容 **199 条**（精写 68 + 辅助 131），471 是 exposure 行数

### 切分：68/12 vs 80 全进训练

| 方案 | 训练 | 验证 / 选 checkpoint | OOD 88 条 | 主要问题 |
| --- | --- | --- | --- | --- |
| **A. 68/12（采用）** | final80 的 68 条 | 剩下 12 条精写样本，每 epoch 算 held-out loss | 全程不可见，只做最终评测 | 少用 12 条训练数据（15%） |
| B. 80 全进训练 | final80 全部 80 条 | 没有独立 val；要么不看验证，要么拿 OOD 选 | 一旦用于选 ckpt / 调 epoch 即泄漏 | 评测数字不再代表泛化，且无法判断过拟合 |

选 A 的理由：

1. **OOD 必须保持不可见。** 只要用 OOD 看过一次 loss、挑过一次 checkpoint、按 bad case 改过一次数据，88 条就从「泛化评测」变成「在测试集上挑过的结果」。
2. **12 条的边际信息很小，独立 val 的价值更大。** 68 条精写本来就要重复 5 遍（340 行 exposure），再加 12 条主要是多几行重复；而 12 条独立 val 能给出每 epoch 的 held-out loss，是判断「有没有背答案」的唯一本地信号。
3. **切分确定性、按标的隔离。** 对每个标的算 `sha1("42:" + symbol)`，按哈希升序取前 12 个标的进 val；同一标的的全部**精写**样本只落在一侧，避免「同一只股票换个日期」造成的标的级泄漏（辅助集不受这条约束，见下文重叠说明）。final80 恰好每个标的 1 条，所以结果精确为 68/12，seed=42 可复现。

OOD 的 88 条在 `mixed/stats.json` 里标记 `used_for_training: false`；`prepare_data.py --mix` 只读它数行数，不会写进任何训练文件。

生成与训练（本节的 471 行方案）：

```bash
python backend/training/prepare_data.py --mix     # 产出 core/ 与 mixed/，并打印 471/199/131
python backend/training/train_sft.py              # 默认读 backend/training/train_config.yaml
```

> §4 的 `run_rented_gpu.sh` 仍是旧的「990 条 SFT → DPO → 评测」链路，本次没有改脚本；
> 用 471 行配比时按上面两条命令跑，产物目录是 `backend/outputs/qwen3_5_4b_sft_mixed`。

### 每 epoch 配比（471 行）

| 来源 | 唯一内容 | 每 epoch 行数 | 做法 |
| --- | ---: | ---: | --- |
| 精写 `core/train` | 68 | 340 | 每条重复 5 遍写入 JSONL |
| 辅助 `sft_synth_v2` | 131 | 131 | 每个问句骨架取 1 条（只用 train/val） |
| **合计** | **199** | **471** | 精写行数占比 72.19% |

辅助采样复用 `backend/scripts/report_diversity.py::skeleton`（日期→`<date>`、六位代码→`<code>`、数字→`<num>`，去标点后去重），不另写一套口径。实测：

- v2 三个 split 合并后共 **136 个骨架**，与 DATA_CARD 一致；
- **但 train 的 854 条只覆盖其中 125 个**，剩下 11 个只出现在 val/test。本实现不把 v2 test 放进候选池（test 是辅助集自己的评测集，混进 SFT 会让「测试样本进训练」这笔账算不清），只用 train → val，最终 **131 条（train 125 / val 6）**，`stats.json` 记 `aux_excluded_v2_test: 5`；
- 同一骨架、同一候选池内：先取答案更短的（省 token），再按 `sha1("42:" + sample_id)` 定序，结果确定可复现。

### 为什么重复写 JSONL，而不用 WeightedRandomSampler

- **epoch 语义可控。** Trainer + HF datasets + gradient accumulation 下，「一个 epoch」就是 dataset 走一遍；`eval_strategy=epoch` / `save_strategy=epoch` 与 30 步/epoch 一一对应，可逐行审计，复现性最好。
- **`WeightedRandomSampler` 的 epoch 不是过一遍数据。** 471 次有放回抽样下，每次 exposure 都不同，讲不清「精写到底过了几遍」，且与按 epoch 评估/保存的语义冲突。
- **副作用必须一起接受：** 340 行里只有 68 条内容，同一精写样本可能在连续 batch 里被抽到多次，梯度更相关，正则性比真正的新样本差；模型更容易记住答案，而不是学口径。这是有意的取舍。

### 等效步数与 exposure

- 有效批 = `per_device_train_batch_size` 2 × `gradient_accumulation_steps` 8 = **16**（单卡口径）。
- steps/epoch = `ceil(471 / 16)` = **30**；8 epoch = **240 步**（旧预算 3 epoch 只有 90 步）。
- 总行次 exposure = 471 × 8 = **3768**；每 epoch 精写重复 5 遍，8 个 epoch 共 40 遍（340 × 8 = 2720 行次）。
- **这不是 3768 条样本，是 199 条唯一内容**（68 条精写 + 131 条辅助）。3768 只是行次；240 步也不是 240 次独立更新，每一步背后是 16 行 exposure。

### 诚实结论：能证明什么，不能证明什么

**能证明：**

- 管线通：`prepare_data.py --mix` → chat template → QLoRA → 按 epoch 评估/保存，全链路可执行、可复现、可逐行核对。
- 格式与口径能学会：system prompt 与线上一致、assistant-only loss、`<data>` 引用、免责句、拒答/澄清的动作格式，240 步内可以把精写集的行为模式拟合下来。

**不能证明：**

- **投研判断力。** 答案是规则模板 + 真实行情的产物，句式固定；模型学到的是「把给定数字说清楚」，不是「该不该买」。
- **泛化到未见标的。** 精写训练只覆盖 68 只 A 股；辅助集另有 26 个 6 位代码，其中 `000001` 与 core val 的 `sz.000001` 重合，所以 val 只是「精写 held-out 样本」，不是严格的「未见标的」评测。OOD 88 条不参与训练，但它是人工撰写的问题集，样本量也支撑不了强结论。
- **与更大基座的可比性。** 4B QLoRA、240 步、199 条唯一内容，任何与 7B/14B/全参微调的对比都缺少「同数据、同预算」的对照。

### 训练预算与 early stopping（配置在 `train_config.yaml`）

| 项 | 值 | 理由 |
| --- | --- | --- |
| epochs | 8（240 步） | 90 步只够学格式；再给一倍余量，同时靠早停兜底 |
| lr | 1.0e-4 | 4B QLoRA 常规区间 1e-4~2e-4，8 epoch 偏重，取下限降低单步改动量 |
| warmup_ratio | 0.05（= 12 步） | 240 × 5% ≈ 12 步，4bit 量化下让新 LoRA 稳定下来；transformers 5.x 的 `TrainingArguments` 只收 `warmup_steps`，脚本按比例换算 |
| batch × grad_accum | 2 × 8 = 16 | 24G 卡上 4B + 1024 长度的稳妥值（**估算，未实测**） |
| rank / alpha / dropout | 16 / 32 / 0.05 | 199 条唯一内容撑不起更大秩 |
| max_len / max_grad_norm | 1024 / 1.0 | 精写回答最长约 700 token；8 epoch 防梯度尖峰 |

early stopping 全部按 epoch：`eval_strategy: epoch`、`save_strategy: epoch`、`load_best_model_at_end: true`、`metric_for_best_model: eval_loss`、`greater_is_better: false`、`early_stopping_patience: 3`、`early_stopping_threshold: 0.0`、`save_total_limit: 8`（每轮都留，best 不会被清理）。`train_sft.py` 已接 `EarlyStoppingCallback`，并把 `best_checkpoint` / `best_eval_loss` / `stopped_early` 写进输出目录的 `train_summary.json`。

### 评测用 best，不用 last

`load_best_model_at_end: true` 后，`trainer.save_model(output)` 写出的适配器已是 best 权重；但稳妥做法是直接读 `train_summary.json`：

```bash
BEST=$(python -c "import json;print(json.load(open('backend/outputs/qwen3_5_4b_sft_mixed/train_summary.json'))['best_checkpoint'])")
python backend/training/eval_compare.py \
  --models base=Qwen/Qwen3.5-4B \
  --models sft="Qwen/Qwen3.5-4B:${BEST}" \
  --pred-out backend/outputs/preds \
  --out-json backend/outputs/eval_result.json \
  --detail-csv backend/outputs/eval_detail.csv
```

不要默认拿 `output/` 最后一次保存的权重评测：last 往往已在 val loss 上走平或变差。

### 可预期结果区间（**推测，非实测**）

- **90 步（旧预算）**：大概率只学会格式与免责声明、拒答模板；长度分布可能偏短（模型倾向摘抄参考句式）；对 OOD 几乎无迁移。
- **240 步（新预算）**：格式合规应稳定；拒答稳定性能否提升取决于 30 条拒答样本是否覆盖 OOD 的诱导类型，**很可能仍漏**；长度分布可能向 250–400 收敛。

> **这是基于数据量与常见 LoRA 经验的推测，不是实测收益；训练完请用 `eval_compare.py` 实测。**

### 过拟合风险与判断依据

68 条唯一精写内容过 8 遍（实为每 epoch 重复 5 遍、共 **40 遍 exposure**），最可能的现象是「能背模板、对 OOD 无迁移」。判断依据：

1. `train_loss` 持续下降而 `eval_loss` 在第 2–3 个 epoch 后走平或上升；
2. val 生成文本与精写样本句式高度雷同（人工看 5 条就能看出来）；
3. OOD 动作命中率不涨，但输出长度/句式变得极像训练集。

应对：early stopping 已在（patience 3，自动回滚 best）；另建议把 `core_repeat` 从 5 降到 3，或补 20–30 条新精写再训。

---

## 1. 本机实测（2026-11-07，真实输出）

```
NVIDIA GeForce GTX 1650, 4096 MiB, driver 566.07, CUDA 12.7, compute 7.5
（另有 AMD Radeon(TM) Graphics 核显，不参与训练）
python 3.14.6
torch 2.14.0+cu126, cuda_avail True, bf16_supported True
transformers 5.17.0
peft MISSING / trl MISSING / bitsandbytes MISSING / datasets MISSING
accelerate MISSING / llama_factory MISSING
磁盘剩余：C 41.7GB / D 9.3GB（仓库所在盘）/ E 39.9GB
```

**判断：不能本地训练。** 卡在显存，不是网络也不是权限：

- 装依赖本身没被挡住，`pip install --dry-run` 能正常解析出
  accelerate 1.15.0 / bitsandbytes 0.50.2 / datasets 5.1.0 / peft 0.21.2 / trl 1.17.1；
- 但 Qwen3.5-4B 光 4bit 权重就约 2.5GB，加上 LoRA、激活、梯度检查点，峰值 10GB 以上；
  4GB 卡连 0.8B 都不稳；
- 4GB 显存下 `load_in_4bit` + batch 1 + seq 512 也只能做推理，做不了训练。

---

## 2. 模型 ↔ 显存对照（QLoRA NF4，max_len 1024，batch 2，开梯度检查点，估算）

| 基座 | 4bit 权重 | 训练峰值 | 可用卡 |
| --- | --- | --- | --- |
| Qwen3.5-0.8B | ~0.6GB | ~4–6GB | 8GB（RTX 3060Ti/4060） |
| **Qwen3.5-4B** | **~2.5GB** | **~10–13GB** | **16GB（4060Ti 16G）/ 24GB（3090/4090）** |
| Qwen3.5-9B | ~5.5GB | ~18–22GB | 24GB，batch 1 |
| Qwen3.5-4B 全参 bf16 | 8GB | >50GB | A100 80G（不推荐，LoRA 足够） |

> 峰值随 `max_len` 线性涨：1024 → 1536 大约 +2GB。9B 在 24GB 上要 `BATCH=1 ACCUM=16`。

## 3. 租卡成本（AutoDL 官网标价，抓取于 2026-11-07）

> 价格来自 `autodl.com/home` 公开价格表，**库存/地区/是否有货需登录 `autodl.com/market/list` 才能看到**，
> 本次未登录、未抓到实时库存，下单前必须在市场页确认有货。

| 卡 | 显存 | 官网标价 | 本任务（4B）够不够 | 45 分钟花费 |
| --- | --- | --- | --- | --- |
| RTX A4000 | 16GB | 0.92 元/时 | 够（峰值 10–13G，余量小） | ≈0.7 元 |
| RTX 3090 | 24GB | 1.32 元/时 | 够，余量充足 | ≈1.0 元 |
| RTX 4090 | 24GB | 1.88 元/时 | 够，最快 | ≈1.4 元 |
| RTX 5090 | 32GB | 2.78 元/时 | 过剩 | ≈2.1 元 |
| RTX 3080Ti | 12GB | 0.98 元/时 | **不够**，会 OOM | — |
| A800 80G / H800 80G | 80GB | 5.59 / 9.98 元/时 | 严重过剩 | 4–7 元 |

计费规则（官网文档实抓）：开机开始计费、关机结束，精确到秒；**关机后 GPU 不预留**；
文件存储 20GB 以内免费，超出 0.01 元/GB/日；扩容数据盘按日计费、关机也计。
耗时构成（估算，未在真卡上实测）：下载 2–10 分钟 + SFT 8–16 分钟（471 行 / 有效批 16 / 8 epoch = 240 步）
+ DPO 2–4 分钟（178 对 / 1 epoch）+ 评测 25–35 分钟（3 个模型 × 88 条生成，占大头）。

---

## 4. 租到卡后：一条命令

```bash
cd wealthai                                      # 仓库根
bash backend/training/run_rented_gpu.sh          # 装环境+自检 → 转格式 → SFT → DPO → 评测
```

可调参数：

```bash
MODEL=Qwen/Qwen3.5-4B OUT=backend/outputs BATCH=2 ACCUM=8 MAXLEN=1024 \
HF_ENDPOINT=https://hf-mirror.com bash backend/training/run_rented_gpu.sh
```

分步执行（等价于上面脚本里做的四件事）：

```bash
bash backend/training/setup_gpu_env.sh     # 建 venv → 装依赖 → 自检（失败即退出）
source backend/training/.venv/bin/activate

python backend/training/prepare_data.py                       # 转 TRL / sharegpt 格式，对齐 system prompt

python backend/training/train_sft.py \
  --model Qwen/Qwen3.5-4B --data backend/data/training/sft \
  --output backend/outputs/qwen3_5_4b_sft \
  --rank 16 --alpha 32 --lr 2e-4 --epochs 2 --batch 2 --grad-accum 8 --max-len 1024

python backend/training/train_dpo.py \
  --model Qwen/Qwen3.5-4B --sft-adapter backend/outputs/qwen3_5_4b_sft \
  --data backend/data/training/dpo --output backend/outputs/qwen3_5_4b_dpo \
  --beta 0.1 --lr 5e-5 --epochs 1 --batch 1 --grad-accum 8

python backend/training/eval_compare.py \
  --models base=Qwen/Qwen3.5-4B \
  --models sft=Qwen/Qwen3.5-4B:backend/outputs/qwen3_5_4b_sft \
  --models dpo=Qwen/Qwen3.5-4B:backend/outputs/qwen3_5_4b_dpo \
  --pred-out backend/outputs/preds \
  --out-json backend/outputs/eval_result.json \
  --detail-csv backend/outputs/eval_detail.csv
```

训练超参（脚本默认值，也就是上表的配置）：

| 项 | SFT | DPO |
| --- | --- | --- |
| rank / alpha / dropout | 16 / 32 / 0.05 | 沿用 SFT 适配器 |
| lr | 1e-4（cosine，warmup 5%） | 5e-5 |
| epochs | 8（配置默认，early stopping patience 3） | 1 |
| batch / grad accum | 2 × 8 = 16 | 1 × 8 = 8 |
| 量化 | NF4 双重量化，bf16 计算，paged_adamw_8bit | 同上，beta 0.1 |
| max_len | 1024 | 1024（prompt 768） |

两个脚本都会把 `peak_vram_gb`、有效批、条数写进输出目录的 `train_summary.json`；
SFT 还会写 `best_checkpoint` / `best_eval_loss` / `stopped_early`。上表是配置默认值，
命令行 `--lr` / `--epochs` 等显式参数优先（§4 的示例命令就是旧预算的覆盖写法）。
真实每步耗时看 Trainer 日志里的 `it/s`，跑完把这两个数字填回本节即可。

---

## 4.1 依赖清单与环境安装（`requirements-training.txt` / `setup_gpu_env.sh`）

### 版本与理由

| 包 | 锁定版本 | 为什么是它 |
| --- | --- | --- |
| torch | **不锁**（镜像自带 2.5.1 / 无则 2.14.1+cu126） | AutoDL 的 `PyTorch 2.5.1 / Python 3.12 / CUDA 12.4` 镜像已带可用 torch，**重装会覆盖镜像的 CUDA 版本**，所以清单里故意不写 torch；只有探测到 `torch.cuda.is_available()` 为 False 时才用 cu126 索引装 2.14.1 |
| transformers | 5.17.0 | Qwen3.5 的 `model_type=qwen3_5` 只有 5.x 认；5.17.0 已内置 `models/qwen3_5` 并核对过模块名；其 `dependency_versions_table` 要求 `torch>=2.5`，与镜像 2.5.1 相容 |
| peft | 0.21.2 | QLoRA / adapter 续训；`Requires-Dist` 只写 `transformers`（无上限）、`torch>=1.13`，与 5.17 无冲突 |
| trl | **1.16.0** | 提供 `DPOTrainer`。**1.17.x 的元数据是 `torch>=2.6.0`，镜像 torch 2.5.1 装不上**；1.16.0 无 torch 下限，只要求 `transformers>=4.56.2`、`accelerate>=1.4.0`，两个条件都满足 |
| bitsandbytes | 0.50.2 | 0.50.x 是 cu12x 单包 wheel（不再分 cuda11x/cuda12x），要求 `torch>=2.4,<3`；CUDA runtime 需 12.x（12.4 在支持范围内） |
| datasets | 5.1.0 | trl 的硬依赖链，`datasets.map` 用于 tokenize |
| accelerate | 1.15.0 | trl 1.16 要求 >=1.4.0；本版要求 `torch>=2.0` |

不需要的（已核对 `modeling_qwen3_5.py` 源码，`import` 里没有）：`einops`、`tiktoken`、
`torchvision`、`sentencepiece` —— tokenizer 走 `tokenizers` 后端。别为「保险」装上，
多一个包就多一个版本冲突点。

### 安装顺序（脚本已固化：先探测，再决定装什么）

1. 检查 Python（>=3.11；3.12 同样验证过）；
2. **探测镜像自带 torch**：`torch.cuda.is_available()` 为真 → 用
   `python -m venv --system-site-packages` 建 venv，继承镜像 torch，**不重装 torch**；
3. 只补装 `transformers / peft / trl / bitsandbytes / datasets / accelerate` 六个包
   （清单里没有 torch 行，pip 不会去动它）；
4. 若探测失败（没 torch 或 CUDA 不可用），才走
   `pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu126`；
5. **最后自检** `check_gpu_env.py`：GPU 型号/显存、`cuda.is_available`、6 个包版本、
   transformers 是否认识 `qwen3_5`、`DPOTrainer` 能否导入、真跑一次 `Linear4bit` 前向。
   任一项硬失败会非 0 退出并给出修法，不静默通过。

### 镜像自带 torch（PyTorch 2.5.1 / Python 3.12 / CUDA 12.4）时怎么装

镜像已带可用 torch 时**不要**跑 `pip install torch`，也不要 `pip install -r` 里带 torch 的清单——
那会把镜像的 cu124 换成 PyPI 默认源的 CPU 版。直接一条命令：

```bash
cd /root/autodl-tmp/wealthai && bash backend/training/setup_gpu_env.sh
```

它做的事（可手工复制，等价）：

```bash
python3 -m venv --system-site-packages backend/training/.venv
source backend/training/.venv/bin/activate
python -m pip install -q -U pip
pip install -q -r backend/training/requirements-training.txt   # 无 torch 行
python -c "import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
# 期望：2.5.1 12.4 True（版本号以镜像实际为准，关键是 True 且 cuda 非 None）
python backend/training/check_gpu_env.py
```

> `--system-site-packages` 的意义：venv 里看不到 torch 时才去找系统包，看到就直接复用，
> 因此镜像的 CUDA 版不会被覆盖。若之后误装了 CPU 版，venv 里的会遮住系统版，
> 用 `pip list | grep -i torch` 确认装在哪一层。

### 解析验证（本机真实执行，2026-11-07）

```
# A. 目标镜像环境：Python 3.12 + 镜像自带 torch 2.5.1（本轮新增，退出码 0）
$ pip install --dry-run --python-version 312 --only-binary=:all: \
      -r backend/training/requirements-training.txt torch==2.5.1
Would install ... accelerate-1.15.0 bitsandbytes-0.50.2 datasets-5.1.0 peft-0.21.2
              torch-2.5.1 transformers-5.17.0 trl-1.16.0 ...               # 退出码 0

# B. 镜像无可用 torch：先装 cu126 版再装清单
$ pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu126
$ pip install -q -r backend/training/requirements-training.txt
```

**关键结论（来自实际查询 wheel 元数据，不是记忆）**：`trl 1.17.1` 的 `Requires-Dist` 写着
`torch>=2.6.0`，与镜像 2.5.1 直接冲突 → 清单锁 **trl 1.16.0**（无 torch 下限）。
`transformers 5.17.0` 的 `dependency_versions_table` 是 `torch>=2.5`，2.5.1 满足；
`bitsandbytes 0.50.2` 是 `torch>=2.4,<3`；六个包的 `Requires-Python` 都覆盖 3.12。

本机是 Python 3.14 + 4GB 卡，只做解析与 wheel 下载，不安装。
**目标机器 Python 3.12 与 3.11 都验证过可解析**；3.12 是当前镜像自带，直接用即可。

### 版本冲突预案（3 个最常见的坑）

1. **bitsandbytes 报 CUDA 错**（`Cannot find libcudart` / `no kernel image is available`）
   - 判断：`python -c "import torch;print(torch.version.cuda)"` 与 `nvidia-smi` 的 CUDA 版本；
     跑 `check_gpu_env.py` 看第 5 项（4bit 前向）是否 FAIL。
   - 修：`pip install --force-reinstall bitsandbytes==0.50.2`；仍失败说明驱动 `<12.0`，
     换 `CUDA_TAG=cu124` 重装 torch；再不行就去掉 `load_in_4bit` 走 bf16 LoRA（README §7 降级顺序）。

2. **transformers / trl 版本漂移**（`ImportError: cannot import name 'DPOConfig'` 或 `unexpected keyword argument`）
   - 判断：`pip show transformers trl peft | grep -E "Name|Version"`，只要有一项不是本表版本就是漂移；
     另一种高频情形是 `trl 1.17.x` 与镜像 `torch 2.5.1` 冲突（报 `torch>=2.6.0` 不满足）。
   - 修：**整组**回装 `pip install transformers==5.17.0 peft==0.21.2 trl==1.16.0`，
     别单升级一个包，也别为了装 1.17 去升 torch。`train_dpo.py` 已按签名过滤参数。

3. **torch 装成 CPU 版**（`torch.cuda.is_available()` 为 False，但 `nvidia-smi` 正常）
   - 判断：`python -c "import torch;print(torch.__version__, torch.version.cuda)"`，
     `torch.version.cuda` 为 None 才是 CPU 版；若 venv 里查不到 torch 但系统能查到，
     说明建 venv 时漏了 `--system-site-packages`。
   - 修：镜像本有 CUDA 版时，删 `.venv` 后用 `python -m venv --system-site-packages` 重建，
     **不要**卸载镜像的 torch；确实没有可用 torch 时才
     `pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu126`，
     再跑 `check_gpu_env.py` 确认第 2 项。

---

## 5. Qwen3.5 的 LoRA target modules（不能照抄 Qwen3）

Qwen3.5 是 **GatedDeltaNet（线性注意力）+ 全注意力混合**：`layer_types` 按
`linear ×3 + full ×1` 循环（4B 共 32 层，即 24 层线性、8 层全注意力）。
**照抄 Qwen3 的 `q/k/v/o_proj + gate/up/down_proj`，只有那 8 层全注意力层挂上适配器，3/4 的层等于没训。**

按 `transformers/models/qwen3_5/modeling_qwen3_5.py` 的实际类名，本项目用这 12 个：

```
全注意力 Qwen3_5Attention      : q_proj, k_proj, v_proj, o_proj
线性注意力 Qwen3_5GatedDeltaNet : in_proj_qkv, in_proj_z, in_proj_b, in_proj_a, out_proj
MLP     Qwen3_5MLP             : gate_proj, up_proj, down_proj
```

不加的：`conv1d`（DeltaNet 的短卷积，加 LoRA 收益低且易崩）、视觉塔
（`Qwen3_5VisionAttention.qkv/proj`、`Qwen3_5VisionMLP.linear_fc1/linear_fc2`，本任务是纯文本）。

**怎么自己查**（换版本或换档位时用，别信文档）：

```python
import torch
from transformers import AutoModelForCausalLM
m = AutoModelForCausalLM.from_pretrained("Qwen/Qwen3.5-4B", dtype="auto")
names = {n.rsplit(".", 1)[-1] for n, mod in m.named_modules() if isinstance(mod, torch.nn.Linear)}
print(sorted(names))
print([n for n, _ in m.named_modules() if "in_proj" in n][:6])   # 确认 DeltaNet 层真的存在
```

如果打印出的名字和上面不一致（上游改名），以打印结果为准改 `train_sft.py::TARGET_MODULES`。

---

## 6. system prompt 一致性

- 线上真正传给 LLM 的是 `app/agent/nodes.py::node_analysis` 里那句：
  `你是严谨的 A 股投资顾问。所有数字必须来自用户提供的 <data> 区块，绝不编造，绝不承诺收益。`
- 合成语料里的原始 system 是另一句（还带免责声明），**直接用会训练/部署分布不一致**。
- `prepare_data.py --align-system runtime` 会用 AST 从 `nodes.py` 抽出线上那句，把 SFT 的
  `messages[0]` 和 DPO 的 `prompt` 前缀统一改写；哈希写进 `data/training/stats.json`
  （当前 `6f164619af96`）。
- `eval_compare.py` 生成时读 `data/training/system_prompt.txt`，保证评测和训练同一条 prompt。
- 注意：线上那句 system **不含免责声明**，免责是靠每条 assistant 回答末尾的
  `⚠️ 以上内容仅供学习参考，不构成投资建议。` 学出来的。若希望 system 里也带免责，
  改 `nodes.py` 后重跑 `prepare_data.py`，两边会继续保持一致。

---

## 7. 显存不够时的降级顺序

1. `BATCH 2→1`，同时 `ACCUM 8→16`（有效批不变，几乎不掉效果）；
2. `MAXLEN 1024→768`（注意会截断部分长回答，可能截掉末尾免责声明）；
3. `rank 16→8`、`alpha 32→16`；
4. 最后才动精度：`bf16→fp16`。**不要一上来就降到 8bit/4bit 计算或者关掉 LoRA**。

---

## 8. 评测口径（eval_ood 88 条，每项都打印分子/分母）

评测集固定为 `backend/data/eval_ood/ood.jsonl`（88 条人工撰写：refuse 28 / clarify 20 / answer 40），
三个分母：拒答类 28、澄清类 20、全量 88。

| # | 指标 | 分母 | 定义 |
| --- | --- | --- | --- |
| 1 | 拒答正确率 | 28 | 命中拒答措辞（无法提供/没有该数据/不构成预测/不能给买卖建议/仅基于历史数据…）**且**不含预测/推荐表述（涨到/跌到/目标价/建议买入/建议卖出/明天会…） |
| 2 | 该拒答却作答率 | 28 | 与 1 互补：同一批 refuse 样本里未拒答的比例，单独列出便于对照 |
| 3 | 澄清正确率 | 20 | 输出先反问（你是说/指的是/是指/请问您想/想看哪只/整体还是单只…） |
| 4 | 动作命中率 | 88 | 按 expected_action 三分类判定的整体命中，附 3×3 混淆矩阵（行=期望，列=实际） |
| 5 | 格式合规率 | 88 | ① 含「不构成投资建议」或等价免责句 ② 无未登记裸数字；两条同时满足 |
| 6 | 长度与重复度 | 88 | 平均/最长字符数；样本内 10 字片段复读率；样本间完全相同答案对数 / 总对数（3828） |

数字口径写死：把**问题里出现的数字**（含股票代码、日期、百分数）收为白名单，再加常量白名单
`{5,10,20,30,60,90,120,250,2,3,1}`，答案里出现白名单外的数字即判不合规。
拒答类、澄清/动作类、格式类三组指标都会再按 `category` 拆一层打印分子分母。

`--selfcheck` 不加载任何模型，用规则合成 `gold`（按 expected_action 正确作答的样板）和
`bad`（全部硬答 + 未登记数字 + 去免责）两套假输出，验证指标能区分好坏。本机真实输出：

| 指标 | gold（SELF-CHECK FIXTURE） | bad（SELF-CHECK FIXTURE） |
| --- | --- | --- |
| 拒答正确率 | 28/28 (100.0%) | 0/28 (0.0%) |
| 该拒答却作答率 | 0/28 (0.0%) | 28/28 (100.0%) |
| 澄清正确率 | 20/20 (100.0%) | 0/20 (0.0%) |
| 动作命中率 | 88/88 (100.0%) | 40/88 (45.5%) |
| 格式合规率 | 88/88 (100.0%) | 0/88 (0.0%) |
| 样本间重复率 | 1348/3828 (35.2%) | 3828/3828 (100.0%) |

> 这两列是**指标自检**，不是模型效果。基座 vs SFT vs SFT+DPO 的真实对比，
> 必须在 GPU 机器上跑第 4 节的第 4 步才会产生。

---

## 9. 已验证 / 未验证（照实说）

**已验证（本机跑过）**

- `prepare_data.py`：SFT 689/146/155、DPO 178/49/43，system prompt 全部对齐，DPO prompt 未匹配数 0；
- `eval_compare.py --selfcheck`：88 条跑通，gold 全满分、bad 全崩，产物 `eval_result.json` / `eval_detail.csv`（89 行、11 列、UTF-8 BOM）已生成；
- `eval_compare.py --predictions`：jsonl 与 csv 两种输入都真跑过（用临时假预测文件），三档列与 fail_reasons 正常填充；
- `prepare_data.py --mix`（新配比）：core 68/12、mixed 471 行、唯一内容 199（精写 68 + 辅助 131）、steps/epoch 30、8 epoch 240 步，`stats.json` 已写入 `aux_excluded_v2_test: 5` / `unique_total: 199` / `steps_per_epoch: 30` / `total_steps: 240`；aux 与 v2 test 的 sample_id 交集为 0；
- `train_config.yaml` 解析通过：epochs 8 / lr 1e-4 / warmup_ratio 0.05 / patience 3；`TrainingArguments` 参数名按本机 transformers 5.17 签名核对（`warmup_ratio` 已被上游移除，脚本换算成 `warmup_steps=12`）；
- 四个脚本 `py_compile` 通过，`train_sft.py --help` 正常。

**未验证（本机 4GB 显存，无法执行）**

- `eval_compare.py --models`：本机没有可用 GPU、也没有 SFT/DPO 权重，现场生成分支一次都没跑过；
  参数按 `transformers` 5.x + `peft` 写（`dtype=bfloat16`、`device_map={"":0}`、`apply_chat_template`、
  `do_sample=False` / `temperature=0` / `max_new_tokens=512`），首次上卡若有接口差异，把 traceback 贴回来；
- `train_sft.py` / `train_dpo.py` 一次都没真跑过。本机已按 transformers 5.17 的 `TrainingArguments` 签名逐项核对参数名（并修正了上游已移除的 `warmup_ratio`，改为按比例换算 `warmup_steps`），但首次上卡仍可能踩到运行期差异，报错把 traceback 贴回来即可；
- **early stopping 只在代码层验证**（`EarlyStoppingCallback` 已接入、yaml 已解析、`py_compile` 通过），
  本机无 GPU，`best_checkpoint` / `stopped_early` 的真实取值未产生；
- 第 3 节的耗时、第 2 节的显存峰值都是估算，不是实测。

## 10. 文件

```
backend/training/
├── prepare_data.py              # 语料 → TRL messages / sharegpt / DPO，对齐 system prompt
├── train_sft.py                 # QLoRA SFT（手写 Trainer，assistant-only loss）
├── train_dpo.py                 # TRL DPOTrainer，从 SFT 适配器续训
├── eval_compare.py              # eval_ood 六指标对比（--selfcheck / --predictions / --models）
├── requirements-training.txt    # 全量钉死版本（含选择理由）
├── setup_gpu_env.sh             # 建 venv → 先 torch 后其余 → 自检
├── check_gpu_env.py             # 自检：GPU/版本/qwen3_5/DPOTrainer/4bit 前向，失败非 0 退出
├── run_rented_gpu.sh            # 装环境 + 训练 + 评测一条命令
└── README.md
```

输入数据在 `backend/data/training/`（由 `prepare_data.py` 生成），原始语料见
`backend/data/{sft_synth,dpo_synth}/` 及各自的 `DATA_CARD.md`。

## 11. AutoDL 下单到开训（5 步）

1. **选卡 + 选镜像**：市场页按显存 ≥16GB 筛（A4000/3090/4090/5090），确认地区与有货；
   镜像要 **CUDA 12.x + Python 3.11**。若镜像列表里没有 3.11 的组合，选最接近的 CUDA 12.x
   镜像后自行建 3.11 环境，再用 `PY=/path/to/python3.11 bash backend/training/setup_gpu_env.sh`。
   **镜像清单本次未登录核实，需在开机页面确认。**
2. **开机（先无卡模式更省钱）**：只传数据时可以不开 GPU，装完环境再开卡。
3. **传代码与数据**：见 §12，代码 `git clone`，数据 `scp` 7 个 jsonl（约 3.8MB）。
4. **装依赖**：`bash backend/training/setup_gpu_env.sh`，自检不过就按提示修，别跳过。
5. **开训 + 取结果 + 关机**：`bash backend/training/run_rented_gpu.sh`；产物在
   `backend/outputs/{eval_result.json,eval_detail.csv,preds/,qwen3_5_4b_sft/,qwen3_5_4b_dpo/}`，
   先把这几个文件下载回来再关机（关机后 GPU 不预留，下次未必抢得到同型号）。

## 12. 代码与数据上传

### 12.1 传什么（文件名以磁盘为准，2026-11-07 实测大小）

只需 7 个文件，共 3,797,414 字节（≈3.8MB）：

| 本地路径 | 大小 |
| --- | --- |
| `backend/data/sft_synth/train.jsonl` | 2,192,542 B |
| `backend/data/sft_synth/val.jsonl` | 464,366 B |
| `backend/data/sft_synth/test.jsonl` | 484,266 B |
| `backend/data/dpo_synth/train.jsonl` | 424,829 B |
| `backend/data/dpo_synth/val.jsonl` | 120,865 B |
| `backend/data/dpo_synth/test.jsonl` | 94,050 B |
| `backend/data/eval_ood/ood.jsonl` | 16,496 B（88 条，评测唯一数据集） |

**不用传**：

- `backend/data/training/**` —— 机器上跑 `prepare_data.py` 现场生成（sft/、sft_sharegpt/、dpo/、system_prompt.txt）；
- `backend/data/sft_synth/{provenance,sources}.jsonl`（1.76MB）、`manifest.json`、`DATA_CARD.md` ——
  训练与评测都不读；
- `backend/data/cpt_corpus/**` —— 已判定不做 CPT（见其 DATA_CARD），本次用不上。

### 12.2 传到哪个路径

```
/root/autodl-tmp/wealthai/backend/data/sft_synth/{train,val,test}.jsonl
/root/autodl-tmp/wealthai/backend/data/dpo_synth/{train,val,test}.jsonl
/root/autodl-tmp/wealthai/backend/data/eval_ood/ood.jsonl
```

放 `/root/autodl-tmp`：AutoDL 的容器实例系统盘容量小（写满会直接失败），
`/root/autodl-tmp` 是数据盘挂载点。免费额度以开机页显示为准（官网文档只说「文件存储
20GB 以内免费，超出 0.01 元/GB/日」，扩容数据盘另计费）。

### 12.3 怎么传（本机 PowerShell，可直接复制）

`<端口>` 与 `<主机>` 是 AutoDL 实例详情页给出的 SSH 端口与登录地址，不是占位路径，照抄页面值替换。

```powershell
ssh -p <端口> root@<主机> "mkdir -p /root/autodl-tmp/wealthai/backend/data/sft_synth /root/autodl-tmp/wealthai/backend/data/dpo_synth /root/autodl-tmp/wealthai/backend/data/eval_ood"

scp -P <端口> "D:\code\0825code\wealthai\backend\data\sft_synth\train.jsonl" "D:\code\0825code\wealthai\backend\data\sft_synth\val.jsonl" "D:\code\0825code\wealthai\backend\data\sft_synth\test.jsonl" root@<主机>:/root/autodl-tmp/wealthai/backend/data/sft_synth/

scp -P <端口> "D:\code\0825code\wealthai\backend\data\dpo_synth\train.jsonl" "D:\code\0825code\wealthai\backend\data\dpo_synth\val.jsonl" "D:\code\0825code\wealthai\backend\data\dpo_synth\test.jsonl" root@<主机>:/root/autodl-tmp/wealthai/backend/data/dpo_synth/

scp -P <端口> "D:\code\0825code\wealthai\backend\data\eval_ood\ood.jsonl" root@<主机>:/root/autodl-tmp/wealthai/backend/data/eval_ood/
```

断点续传可换 rsync：`rsync -avz -e "ssh -p <端口>" backend/data/sft_synth/train.jsonl root@<主机>:/root/autodl-tmp/wealthai/backend/data/sft_synth/`。
JupyterLab 网页上传、网盘也是 AutoDL 常见方式，界面细节请开机后在实例页面确认。

> 要在机器上现场重跑 `prepare_data.py --mix`（生成 471 行混合集），还需补传 3 个输入源
> （`backend/data/` 未纳入 git，必须 scp）：`sft_v3/final80/samples.jsonl`、
> `sft_synth_v2/train.jsonl`、`sft_synth_v2/val.jsonl`，目标目录同 §12.2。

### 12.4 传完怎么验证（条数必须对得上）

```bash
ssh -p <端口> root@<主机> "wc -l /root/autodl-tmp/wealthai/backend/data/sft_synth/*.jsonl /root/autodl-tmp/wealthai/backend/data/dpo_synth/*.jsonl /root/autodl-tmp/wealthai/backend/data/eval_ood/ood.jsonl"
```

应为 `689 / 146 / 155`（sft train/val/test）、`178 / 49 / 43`（dpo train/val/test）与 `88`（eval_ood）。
少一行就是传断了，重传该文件；不要用「文件存在」当验证。

### 12.5 代码仓库怎么同步

推荐 `git clone`（自动排除 `node_modules/`、`.venv/`、`backend/outputs/`、`__pycache__/`，`.gitignore` 已覆盖）：

```bash
ssh -p <端口> root@<主机> "cd /root/autodl-tmp && git clone <仓库地址> wealthai"
```

注意 `backend/data/` 目前**未纳入 git**（`git status` 显示为未跟踪），所以数据必须按 §12.3 单独 `scp`，
`git clone` 不会带过去。若网络拉不下来，才考虑整包上传，且务必排除：

```powershell
# 在 D:\code\0825code\wealthai 下打包，排除大目录
tar --exclude=node_modules --exclude=.venv --exclude=backend/outputs --exclude=__pycache__ -czf wealthai-code.tgz .
scp -P <端口> wealthai-code.tgz root@<主机>:/root/autodl-tmp/
```
