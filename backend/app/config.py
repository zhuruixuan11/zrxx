"""应用配置（pydantic-settings，.env 自动加载）。"""
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "衡策"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True
    # SQL 日志单独开关：开发调试时可开，默认关闭（避免持仓等敏感数据进日志）
    SQL_ECHO: bool = False

    # 日志落盘（相对 backend 目录）；置空则只输出到终端
    LOG_FILE: Optional[str] = "logs/hengce.log"
    LOG_LEVEL: str = "INFO"

    # 数据库：本地开发用 SQLite；生产换 PostgreSQL 只需改 URL
    DATABASE_URL: str = "sqlite+aiosqlite:///./hengce.db"
    # SQLite 写锁等待时长（毫秒）：WAL 下写-写仍互斥，0 会立刻抛 database is locked
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    # RAG 三路召回：词法(bigram) + BM25(jieba) + 向量(bge-small-zh，本地离线)
    # 模型目录不存在或加载失败时自动降级为「词法 + BM25」，并在接口 fusion 里标注
    RAG_EMBED_MODEL_PATH: str = "E:/hengce/models/bge-small-zh-v1.5"
    RAG_EMBED_ENABLED: bool = True
    RAG_RRF_K: int = 60

    # JWT
    JWT_SECRET_KEY: str = "change-me-in-production-please-use-32-chars-minimum"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = 720  # 12h

    # LLM Provider：mock/deepseek/openai/qwen；未配置 API Key 时自动回 Mock
    LLM_PROVIDER: str = "mock"
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    QWEN_API_KEY: Optional[str] = None
    QWEN_MODEL: str = "qwen-max"
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 模型微调（阿里百炼 DashScope，仅华北2-北京地域可用；复用 QWEN_API_KEY）
    QWEN_FINETUNE_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    QWEN_FINETUNED_MODEL: Optional[str] = None  # 微调产出的可调用模型名
    FINETUNE_DIR: str = "./data/finetune"       # 导出的训练 jsonl 落盘目录

    # 行情数据源为 baostock + 东财实时盘口 + akshare，均免费且无需 Token，无需配置

    # 到价预警（后台轮询；仅交易时段检查，避免无谓拉行情）
    ALERT_ENABLED: bool = True
    ALERT_POLL_INTERVAL_SECONDS: int = 60
    ALERT_TRADING_HOURS_ONLY: bool = True

    # 预警邮件推送（可选；不配置则只做站内提醒）
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 465
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None

    # 限流
    AGENT_CHAT_RATE_LIMIT: str = "20/minute"
    MARKET_RATE_LIMIT: str = "60/minute"
    LOGIN_RATE_LIMIT: str = "20/minute"

    CORS_ORIGINS: list = Field(default_factory=lambda: [
        "http://localhost:5173", "http://localhost:3000",
    ])

    # Agent 免责声明（必须在所有 AI 回复里附加）
    AGENT_DISCLAIMER: str = (
        "本 AI 助手提供的分析和建议仅供学习参考，不构成任何投资建议。"
        "投资有风险，入市需谨慎。"
    )

    # 合规审查（衡策 → Legal-Agent，仅投资分析/风险评估送审）
    # 关闭或 Legal-Agent 不可用时，分析结果照常返回，只在末尾标注「未审」
    COMPLIANCE_ENABLED: bool = True
    LEGAL_AGENT_URL: str = "http://127.0.0.1:8021"
    LEGAL_AGENT_TIMEOUT_SECONDS: float = 5.0

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
