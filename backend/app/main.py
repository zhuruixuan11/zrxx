"""衡策 FastAPI 入口。"""
import asyncio
from contextlib import asynccontextmanager

import fastapi
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_ipaddr

from .config import get_settings
from .database import init_tables
from .api.alerts import router as alerts_router
from .api.auth import router as auth_router
from .api.agent import router as agent_router
from .api.backtest import router as backtest_router
from .api.datasets import router as datasets_router
from .api.market import prewarm_caches
from .api.market import router as market_router
from .api.news import router as news_router
from .api.portfolio import router as portfolio_router
from .api.profile import router as profile_router
from .api.signals import router as signals_router
from .services.alerts import start_monitor, stop_monitor

settings = get_settings()
limiter = Limiter(key_func=get_ipaddr)

# 日志落盘（按大小轮转，保留 7 天），便于事后排障；配置为空则只输出到终端
if settings.LOG_FILE:
    logger.add(
        settings.LOG_FILE,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
        level=settings.LOG_LEVEL,
    )


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    """应用启动时自动建表，并拉起到价预警轮询。"""
    logger.info(f"衡策 启动中 (version={settings.APP_VERSION}, LLM={settings.LLM_PROVIDER})")
    if settings.JWT_SECRET_KEY.startswith("change-me"):
        logger.warning("JWT_SECRET_KEY 仍为默认值，请尽快替换为随机串（生产环境必须修改）")
    await init_tables()
    start_monitor()
    warmup_task = asyncio.create_task(prewarm_caches())
    logger.info("衡策 启动完成 ✅")
    try:
        yield
    finally:
        warmup_task.cancel()
        stop_monitor()


app = fastapi.FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="衡策 · A 股智能投研平台（LangGraph + RAG + baostock 行情 + 策略回测）",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由（统一 /api/ 前缀由各 router 自己声明）
app.include_router(alerts_router)
app.include_router(profile_router)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(backtest_router)
app.include_router(datasets_router)
app.include_router(market_router)
app.include_router(news_router)
app.include_router(portfolio_router)
app.include_router(signals_router)

@app.get("/", include_in_schema=False)
async def index():
    """后端只提供 API，页面由前端工程提供（开发环境 Vite，生产环境静态托管）。"""
    return {
        "code": 0,
        "msg": "衡策 后端服务运行中",
        "data": {"frontend": "http://127.0.0.1:5173", "docs": "/api/docs", "api": "/api/"},
    }


@app.get("/api/")
async def root_api():
    """API 总览（/api/ 前缀，不与前端根路径冲突）。"""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/api/docs",
        "endpoints": {
            "注册": "/api/auth/register",
            "登录": "/api/auth/login",
            "当前用户": "/api/auth/me",
            "投资组合": "/api/portfolio/summary",
            "持仓": "/api/portfolio/holdings",
            "行情": "/api/market/quote/{symbol}",
            "历史K线": "/api/market/history/{symbol}",
            "全景选股": "/api/market/screener",
            "资讯快讯": "/api/news/flash",
            "Agent对话": "/api/agent/chat",
            "策略回测": "/api/backtest/strategies",
            "回测历史": "/api/backtest/history",
            "多策略对比": "/api/backtest/compare",
            "到价预警": "/api/alerts",
            "投资画像": "/api/profile",
            "健康检查": "/api/health",
        },
        "disclaimer": settings.AGENT_DISCLAIMER,
    }


@app.get("/api/health")
async def health():
    return {
        "code": 0, "msg": "ok",
        "data": {"status": "running", "version": settings.APP_VERSION,
                 "llm_provider": settings.LLM_PROVIDER,
                 "database_url": settings.DATABASE_URL[:30] + "..."},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=settings.DEBUG)
