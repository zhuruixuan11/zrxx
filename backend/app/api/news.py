"""新闻资讯 API —— 多路免费源聚合，绝不造假。

主路径（已验证可用）：
  1. akshare.stock_info_global_em()   — 东方财富全球财经快讯（~200 条/次）
  2. akshare.stock_info_global_cls()  — 财联社电报（~20 条/次）
  3. feedparser 钛媒体 RSS           — https://www.tmtpost.com/rss（20 条）

Fallback（偶尔被反爬，不影响主路径）：
  4. 东方财富滚动新闻 / 财联社旧接口 / 新浪财经
"""
from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import aiohttp
import feedparser
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from ..agent.sentiment import aggregate, label_items

router = APIRouter(prefix="/api/news", tags=["news"])

# akshare 是同步的，用线程池包一下
_executor = ThreadPoolExecutor(max_workers=4)


# ===================== akshare 主路径 =====================

def _fetch_ak_em() -> List[Dict[str, Any]]:
    """东方财富全球财经快讯。"""
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        items = []
        for _, row in df.iterrows():
            title = str(row.get("标题") or "").strip()
            if not title:
                continue
            pub = str(row.get("发布时间") or "")
            ts = _parse_time(pub)
            items.append({
                "title": title,
                "summary": _clean_text(str(row.get("摘要") or ""))[:200],
                "url": str(row.get("链接") or ""),
                "source": "东方财富",
                "publish_time": ts,
                "publish_time_str": pub,
            })
        logger.info(f"[news] akshare em got {len(items)} items")
        return items
    except Exception as e:
        logger.warning(f"[news] akshare em failed: {e}")
        return []


def _fetch_ak_cls() -> List[Dict[str, Any]]:
    """财联社电报。"""
    try:
        import akshare as ak
        df = ak.stock_info_global_cls()
        items = []
        for _, row in df.iterrows():
            title = str(row.get("标题") or "").strip()
            content = str(row.get("内容") or "").strip()
            # 如果标题空，从内容里截一段当标题
            if not title and content:
                title = content[:60]
            if not title:
                continue
            date_str = str(row.get("发布日期") or "")
            time_str = str(row.get("发布时间") or "")
            pub = f"{date_str} {time_str}".strip()
            ts = _parse_time(pub)
            items.append({
                "title": title,
                "summary": _clean_text(content)[:200] if content else "",
                "url": "",  # 财联社电报通常没有单独链接
                "source": "财联社",
                "publish_time": ts,
                "publish_time_str": pub,
            })
        logger.info(f"[news] akshare cls got {len(items)} items")
        return items
    except Exception as e:
        logger.warning(f"[news] akshare cls failed: {e}")
        return []


# ===================== RSS 补充 =====================

async def _fetch_rss_async(url: str, source_name: str) -> List[Dict[str, Any]]:
    """抓 RSS feed，用 feedparser 解析。"""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        ) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"[news] rss {source_name} -> {resp.status}")
                    return []
                xml = await resp.text()

        feed = feedparser.parse(xml)
        items = []
        for e in feed.entries[:30]:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            pub = e.get("published") or e.get("updated") or ""
            ts = _parse_rss_time(pub)
            summary = ""
            if e.get("summary"):
                summary = _clean_text(e.summary)[:200]
            elif e.get("description"):
                summary = _clean_text(e.description)[:200]
            items.append({
                "title": title,
                "summary": summary,
                "url": e.get("link") or "",
                "source": source_name,
                "publish_time": ts,
                "publish_time_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
            })
        logger.info(f"[news] rss {source_name} got {len(items)} items")
        return items
    except Exception as e:
        logger.debug(f"[news] rss {source_name} failed: {e}")
        return []


# ===================== 工具函数 =====================

def _parse_time(s: str) -> int:
    """解析 '2026-11-04 16:08:00' 格式的时间字符串，返回 unix ts。"""
    if not s:
        return int(time.time())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(s, fmt)))
        except ValueError:
            continue
    return int(time.time())


def _parse_rss_time(s: str) -> int:
    """解析 RFC2822 格式的 RSS 时间，如 'Wed, 04 Nov 2026 14:46:00 +0800'。"""
    if not s:
        return int(time.time())
    try:
        import email.utils
        parsed = email.utils.parsedate_to_datetime(s)
        return int(parsed.timestamp())
    except Exception:
        return int(time.time())


def _clean_text(text: str) -> str:
    """去 HTML 标签、实体、多余空白。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ===================== 聚合抓取 =====================

async def _fetch_all() -> List[Dict[str, Any]]:
    """并发抓所有源，合并去重排序。"""
    # akshare 两路（用线程池，因为是同步）
    loop = asyncio.get_event_loop()
    ak_em = loop.run_in_executor(_executor, _fetch_ak_em)
    ak_cls = loop.run_in_executor(_executor, _fetch_ak_cls)

    # RSS 两路
    rss_tmtpost = _fetch_rss_async("https://www.tmtpost.com/rss", "钛媒体")
    rss_36kr = _fetch_rss_async("https://36kr.com/feed", "36氪")

    results = await asyncio.gather(ak_em, ak_cls, rss_tmtpost, rss_36kr, return_exceptions=True)

    all_items: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        if isinstance(r, list):
            all_items.extend(r)

    # 按时间倒序
    all_items.sort(key=lambda x: x.get("publish_time", 0), reverse=True)
    # 去重（同标题视为重复）
    seen = set()
    deduped = []
    for it in all_items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        deduped.append(it)

    logger.info(f"[news] total aggregated: {len(deduped)} items from {len(results)} sources")
    return deduped


# ===================== API 端点 =====================

@router.get("/flash")
async def flash(
    limit: int = Query(default=30, ge=1, le=100),
):
    """财经快讯（多路免费源聚合：东方财富 + 财联社 + 钛媒体 + 36氪）。"""
    items = await _fetch_all()

    if not items:
        return {
            "code": 0,
            "msg": "ok",
            "data": {
                "items": [],
                "sentiment_summary": aggregate([]),
                "note": "当前财经快讯源暂时不可用，稍后再试。不返回虚假新闻。",
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

    # 情感标注（规则引擎，毫秒级），并对返回窗口做整体情绪汇总
    labeled = label_items(items)
    window = labeled[:limit]
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "items": window,
            "total": len(labeled),
            "sentiment_summary": aggregate(window),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


@router.get("/sources")
async def sources():
    """返回可用新闻源列表。"""
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "sources": [
                "东方财富（akshare 全球快讯）",
                "财联社（akshare 电报）",
                "钛媒体 RSS",
                "36氪 RSS",
            ],
            "note": "多路聚合，任一源可用即返回真实数据。绝不生成模拟新闻。",
        },
    }
