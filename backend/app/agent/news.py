"""新闻抓取工具——给 Agent analysis 节点注入实时财经快讯做决策参考。

返回格式：{title, source, publish_time_str, url?}[]
多路聚合，某一路挂了不影响其他路。绝不返回假新闻。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List

import aiohttp

from loguru import logger

from .sentiment import aggregate, label_items

async def fetch_news(limit: int = 15) -> List[Dict[str, Any]]:
    """并发抓多路财经快讯。全部不可用时返回空数组（不造假）。"""
    tasks = [
        _fetch_cls_telegraph(),       # 财联社电报（可能被反爬）
        _fetch_eastmoney_news(),      # 东方财富新闻列表
        _fetch_sina_roll_news(),      # 新浪滚动新闻
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: List[Dict[str, Any]] = []
    for raw in results:
        if isinstance(raw, Exception) or not raw:
            continue
        if isinstance(raw, list):
            all_items.extend(raw)

    # 去重 + 按时间倒序
    seen = set()
    deduped = []
    for it in sorted(all_items, key=lambda x: x.get("ts", 0), reverse=True):
        title = it.get("title", "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        deduped.append({
            "title": title,
            "summary": it.get("summary", ""),
            "source": it.get("source", "未知"),
            "publish_time": it.get("time_str", ""),
            "url": it.get("url", ""),
        })
        if len(deduped) >= limit:
            break

    # 规则引擎情感标注（毫秒级，不额外调 LLM）
    return label_items(deduped)


async def _fetch_json(url: str, referer: str, timeout: float = 5.0) -> Any:
    """带 Referer 头的 JSON 抓取。"""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
            },
        ) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        logger.debug(f"news fetch error [{url[:40]}]: {e}")
        return None


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150]


def _fmt_ts(ts: int | float | str) -> tuple[int, str]:
    """返回 (timestamp, 格式化字符串)。"""
    try:
        t = float(ts) if ts else 0
        ts_int = int(t)
        # 如果 ts 是毫秒级（>10^12），除以 1000
        if ts_int > 10 ** 12:
            ts_int = ts_int // 1000
        s = time.strftime("%m-%d %H:%M", time.localtime(ts_int)) if ts_int else ""
        return ts_int, s
    except Exception:
        return 0, ""


# ---------- 各路解析 ----------

async def _fetch_cls_telegraph() -> List[Dict[str, Any]]:
    """财联社电报。"""
    j = await _fetch_json(
        "https://www.cls.cn/nodeapi/updateTelegraph?app=CailianpressWeb&os=web&sv=8.4.6&rn=30",
        "https://www.cls.cn/telegraph",
    )
    if not j:
        return []
    data_list = []
    if isinstance(j.get("data"), dict) and isinstance(j["data"].get("roll_data"), list):
        data_list = j["data"]["roll_data"]
    elif isinstance(j.get("data"), list):
        data_list = j["data"]

    items = []
    for e in data_list:
        if not isinstance(e, dict):
            continue
        title = e.get("title") or e.get("brief") or ""
        if not title:
            continue
        ts, ts_str = _fmt_ts(e.get("ctime"))
        items.append({
            "title": title,
            "summary": _clean_text(e.get("content") or ""),
            "source": "财联社",
            "ts": ts,
            "time_str": ts_str,
            "url": e.get("shareurl") or "",
        })
    return items


async def _fetch_eastmoney_news() -> List[Dict[str, Any]]:
    """东方财富滚动新闻。"""
    j = await _fetch_json(
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=350&order=1&page_index=1&page_size=30",
        "https://finance.eastmoney.com/",
    )
    if not j:
        return []
    data_list = []
    d = j.get("data")
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, list):
                data_list = v
                break
    elif isinstance(d, list):
        data_list = d

    items = []
    for e in data_list:
        if not isinstance(e, dict):
            continue
        title = e.get("title") or ""
        if not title:
            continue
        ts, ts_str = _fmt_ts(e.get("showTime") or e.get("time"))
        # 东方财富的 showTime 有时是字符串 "2026-11-04 15:30:00"
        if isinstance(e.get("showTime"), str) and not ts:
            try:
                struct = time.strptime(e["showTime"], "%Y-%m-%d %H:%M:%S")
                ts = int(time.mktime(struct))
                ts_str = time.strftime("%m-%d %H:%M", struct)
            except Exception:
                pass
        items.append({
            "title": title,
            "summary": _clean_text(e.get("digest") or e.get("content") or ""),
            "source": "东方财富",
            "ts": ts,
            "time_str": ts_str,
            "url": e.get("url") or e.get("url_unique") or "",
        })
    return items


async def _fetch_sina_roll_news() -> List[Dict[str, Any]]:
    """新浪财经滚动。"""
    j = await _fetch_json(
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page=1",
        "https://finance.sina.com.cn/",
    )
    if not j:
        return []
    data_list = []
    result = j.get("result") or j.get("data") or {}
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list):
                data_list = v
                break
    elif isinstance(result, list):
        data_list = result

    items = []
    for e in data_list:
        if not isinstance(e, dict):
            continue
        title = e.get("title") or e.get("name") or ""
        if not title:
            continue
        ts, ts_str = _fmt_ts(e.get("ctime") or e.get("time") or e.get("createtime"))
        items.append({
            "title": title,
            "summary": _clean_text(e.get("intro") or e.get("summary") or ""),
            "source": "新浪财经",
            "ts": ts,
            "time_str": ts_str,
            "url": e.get("url") or e.get("link") or "",
        })
    return items


def format_news_for_prompt(items: List[Dict[str, Any]], max_items: int = 10) -> str:
    """把新闻条目格式化成给 LLM 的 prompt 片段。无新闻则返回友好提示。"""
    if not items:
        return "（暂未获取到实时财经快讯，以下分析仅基于行情数据和投资组合。）"

    tag = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
    lines = ["【最新财经快讯】"]
    for i, it in enumerate(items[:max_items], 1):
        src = it.get("source", "")
        tm = it.get("publish_time", "")
        title = it.get("title", "")
        label = tag.get(it.get("sentiment", ""), "")
        prefix = f"[{label}] " if label else ""
        lines.append(f"  {i}. {prefix}[{src}] {title}  {'('+tm+')' if tm else ''}")

    summary = aggregate(items[:max_items])
    if summary["total"]:
        lines.append(
            f"（快讯整体情绪：{summary['label']}，利好 {summary['bullish']} 条 / "
            f"利空 {summary['bearish']} 条 / 中性 {summary['neutral']} 条）"
        )
    return "\n".join(lines)
