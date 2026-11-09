#!/usr/bin/env python
"""衡策 CPT 语料构造器（只用仓库自有内容，不联网、不引入第三方文本）。

产出：
  data/cpt_corpus/corpus.txt          纯文本（段落之间空行 + 来源标记）
  data/cpt_corpus/corpus.jsonl        逐段结构化（id/source/type/text/chars）
  data/cpt_corpus/manifest.json       段数、字数、token 估算、来源分布

说明：基座模型（如 Qwen3.5）已完成预训练，本脚本产出的是**可选的领域继续预训练**
语料，不是从零预训练数据。当前仓库自有文本量远低于 CPT 的有效规模，详见 DATA_CARD.md。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

MD_SOURCES = [
    (REPO / "README.md", "doc"),
    (BACKEND / "README.md", "doc"),
    (REPO / "frontend" / "README.md", "doc"),
    (BACKEND / "scripts" / "README_sft_synth.md", "doc"),
]

_CODE_FILES = {
    "strategy_doc": sorted((BACKEND / "app" / "backtest_engine" / "strategies").glob("*.py")),
    "indicator_doc": [BACKEND / "app" / "agent" / "indicators.py"],
    "schema_doc": sorted((BACKEND / "app" / "models").glob("*.py")),
    "prompt": [BACKEND / "app" / "agent" / "nodes.py"],
}

_MIN_LEN = 40


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO)).replace("\\", "/")
    except ValueError:
        return p.name


def md_segments(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", raw)
    out = []
    for b in blocks:
        text = b.strip()
        # 去掉纯代码块、纯标题、徽章行
        if len(text) < _MIN_LEN:
            continue
        if text.startswith("```") and text.count("```") <= 2:
            continue
        if re.fullmatch(r"[\#\-\*\s\|:a-zA-Z0-9_\./]+", text) and "。" not in text:
            continue
        out.append({"source": _rel(path), "type": "doc", "text": text})
    return out


def _docstring(node) -> str:
    return ast.get_docstring(node) or ""


def code_segments(path: Path, kind: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    rel = _rel(path)
    out: list[dict] = []

    mod_doc = _docstring(tree)
    if len(mod_doc) >= _MIN_LEN:
        out.append({"source": rel, "type": kind, "text": mod_doc.strip()})

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cls_doc = _docstring(node)
            attrs = []
            for sub in node.body:
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                        attrs.append(f"{sub.targets[0].id} = {sub.value.value}")
            lines = [f"{node.name}："]
            if cls_doc:
                lines.append(cls_doc.strip())
            if attrs:
                lines.append("属性：" + "；".join(attrs) + "。")
            text = "\n".join(lines)
            if len(text) >= _MIN_LEN:
                out.append({"source": rel, "type": kind, "text": text})
    if kind == "prompt":
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 80:
                text = node.value.strip()
                if "你" in text or "请" in text:
                    out.append({"source": rel, "type": "prompt", "text": text})
    return out


def strategy_segments() -> list[dict]:
    """从策略模块的类名/参数/信号规则生成策略说明（模板化描述，事实来自代码）。"""
    out: list[dict] = []
    base = BACKEND / "app" / "backtest_engine" / "strategies"
    names = {
        "ma_cross": "均线金叉死叉", "ema_sma_bias": "EMA/SMA 偏离", "triple_ma": "三均线系统",
        "macd": "MACD 金叉死叉", "adx_macd": "ADX 过滤 + MACD", "donchian_breakout": "唐奇安通道突破",
        "turtle": "海龟交易法则", "volume_breakout": "量价突破", "boll_breakout": "布林带突破",
        "boll_reversion": "布林带回归", "rsi": "RSI 超卖超买", "rsi_ma200": "RSI + MA200 趋势过滤",
        "kdj": "KDJ 金叉死叉", "stochastic": "随机指标 STOCH", "cci": "CCI 顺势指标",
        "williams_r": "威廉指标 W%R", "momentum_roc": "动量 ROC", "obv_cross": "OBV 能量潮",
        "vwap_deviation": "VWAP 偏离", "ma_cross_atr_stop": "均线 + ATR 止损",
        "vol_target_ma_cross": "波动率目标仓位", "kelly_ma_cross": "凯利公式仓位",
        "cross_section_momentum": "截面动量（多标的）",
    }
    for f in sorted(base.glob("*.py")):
        if f.name == "__init__.py":
            continue
        src = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', src, re.M)
        key = m.group(1) if m else f.stem
        params = sorted(set(re.findall(r'params\.get\(\s*"([a-z_]+)"\s*,\s*([^\)]+)\)', src)))
        cls = re.findall(r"class\s+(\w+)\(BacktestStrategy\)", src)
        param_text = "、".join(f"{p}（默认 {d.strip()}）" for p, d in params) or "无显式参数"
        text = (
            f"策略「{names.get(key, key)}」（策略标识 {key}）用于衡策的日线回测。"
            f"实现类 {', '.join(cls) or f.stem}，定义于 backtest_engine/strategies/{f.name}。"
            f"可调参数：{param_text}。"
            "信号在每个交易日收盘后计算，回测引擎在下一根 K 线开盘按整手成交，"
            "并计入佣金与卖出印花税；回测结果仅反映历史区间表现，不构成投资建议。"
        )
        out.append({"source": _rel(f), "type": "strategy_doc", "text": text})
    return out


def collect() -> list[dict]:
    rows: list[dict] = []
    for path, kind in MD_SOURCES:
        rows.extend(md_segments(path) if kind == "doc" else [])
    for kind, files in _CODE_FILES.items():
        for f in files:
            if f.name == "__init__.py":
                continue
            rows.extend(code_segments(f, kind))
    rows.extend(strategy_segments())

    seen: set[str] = set()
    uniq: list[dict] = []
    for r in rows:
        key = re.sub(r"\s+", "", r["text"])[:200]
        if not key or key in seen:
            continue
        seen.add(key)
        r["text"] = r["text"].strip()
        r["chars"] = len(r["text"])
        uniq.append(r)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="衡策 CPT 语料构造器（仅仓库自有文本）")
    ap.add_argument("--out", default=str(BACKEND / "data" / "cpt_corpus"))
    args = ap.parse_args()

    rows = collect()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, r in enumerate(rows, 1):
        r["id"] = f"cpt-{i:05d}"

    with open(out_dir / "corpus.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(out_dir / "corpus.txt", "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(f"### 来源：{r['source']}（{r['type']}）\n{r['text']}\n\n")

    chars = sum(r["chars"] for r in rows)
    # 中文按 1 字≈0.7 token 粗估（偏保守取 0.8，避免高估）
    est_tokens = int(chars * 0.8)
    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "segments": len(rows),
        "chars": chars,
        "estimated_tokens": est_tokens,
        "estimate_method": "中文按每字 0.8 token 粗估（偏保守）",
        "by_type": dict(Counter(r["type"] for r in rows)),
        "by_source_top": dict(Counter(r["source"] for r in rows).most_common(15)),
        "excluded_on_purpose": [
            "第三方行情数据（baostock/东财/新浪）",
            "第三方研报、新闻、公告原文",
            "含具体个股收益数字的回测结论（避免把行情数据写进语料）",
        ],
        "note": "仅含仓库自有文本：文档段落、策略说明、指标/模型说明、内置 prompt。",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"out_dir": str(out_dir), **{k: manifest[k] for k in
                     ("segments", "chars", "estimated_tokens", "by_type")}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
