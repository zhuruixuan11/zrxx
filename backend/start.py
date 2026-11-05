"""启动脚本（cd backend && python start.py）。"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./hengce.db")
os.environ.setdefault("LLM_PROVIDER", "mock")

import uvicorn

uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=True, log_level="info")
