# 衡策 · A 股智能投研平台 —— 后端

基于 **FastAPI + LangGraph + RAG + baostock/akshare** 的 A 股智能投研后端。
面向个人投资者的研究、选股、回测与盯盘场景，提供对话式投研助手、23 套策略回测、
到价预警与新闻情绪分析。

---

## 快速开始

```bat
cd backend
pip install -r requirements.txt
copy .env.example .env   :: 默认 LLM_PROVIDER=mock（不配 Key 也能跑通全链路）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

| 入口 | 地址 |
|------|------|
| Swagger 文档 | http://127.0.0.1:8001/api/docs |
| 健康检查 | http://127.0.0.1:8001/api/health |
| API 总览 | http://127.0.0.1:8001/api/ |

默认演示账号：登录页输入任意用户名/密码即可注册（注册时自动创建 10 万初始资金的组合）。

---

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| Web 框架 | FastAPI + Uvicorn | async 全链路 |
| Agent 核心 | LangGraph | 意图路由 + 并行 fan-out/fan-in |
| LLM | DeepSeek / OpenAI / 通义千问 | 未配置 Key 时自动 Mock |
| 数据库 | SQLAlchemy 2.0 async + aiosqlite | SQLite（`hengce.db`），生产可换 PostgreSQL |
| 行情 | baostock + akshare + 东财实时 | 全部免费、无需 Token |
| 资讯 | akshare 快讯 + RSS（钛媒体、36氪） | 后台聚合 + 中文情绪标注 |
| 回测 | 自研引擎 + 23 套内置策略 | baostock 前复权日线，次日开盘成交 |
| 调度 | asyncio 后台任务（FastAPI lifespan 托管） | 交易时段感知的到价预警轮询 |

前端为独立的 Vue 3 工程（见 `../frontend`），通过 Vite 代理访问 `/api/*`。

---

## 目录结构

```
app/
├── main.py              # FastAPI 入口、lifespan、路由注册
├── config.py            # pydantic-settings 配置
├── database.py          # 异步引擎 + 建表 + SQLite 增量迁移
├── security.py          # JWT / 密码哈希
├── api/                 # 路由层：auth / portfolio / market / news / agent
│                        #          signals / backtest / alerts / profile / datasets
├── agent/               # LangGraph 节点、RAG、资讯抓取、情绪、画像、行情工具
├── backtest_engine/     # 策略基类 + 注册表 + 指标 + 运行器 + 23 个策略
├── models/              # SQLAlchemy 模型
├── llm/client.py        # 多 Provider LLM 客户端
└── services/alerts.py   # 到价预警后台轮询
```

---

## Agent 架构

```
classify_intent（关键词 + LLM）
        │
        ├─ clarification      意图不明 → 反问
        ├─ portfolio          组合查询
        ├─ market             行情查询
        ├─ knowledge          RAG 知识库
        └─ investment_analysis（并行 fan-out）
                 ├─ get_portfolio
                 ├─ get_quote
                 ├─ rag_retrieve
                 └─ get_profile_summary   ← 用户投资画像
                        │
                        ▼
              synthesize_analysis（汇总 + LLM 生成）
                        │
                        ▼
              risk_position（按风险偏好给出仓位建议）
                        │
                        ▼
              disclaimer（强制追加免责声明）
```

- **RAG**：本地 `knowledge.json` + 数据库中已发布的数据集，关键词检索合并排序。
- **画像**：`risk_level` 决定建议仓位上限（保守 10%/50%、稳健 20%/70%、均衡 25%/80%、激进 35%/90%）。
- **新闻情绪**：规则引擎（财经词表 + 否定前缀 + 语义反转 + 正则兜底），毫秒级、可解释、零成本。

---

## 回测引擎

- 策略基类 `BacktestStrategy`：`enrich` / `signal` / `risk_exit` / `position_fraction`
- 通过 `@register_strategy` 装饰器自动注册，`backtest_engine/strategies/` 下共 **23 套**
- 撮合规则：**信号次日开盘成交**（避免未来函数）、佣金万三、卖出印花税千一、100 股整手
- 资金不足一手时返回 `warning` 提示（不再静默返回 0 笔交易）
- 输出：交易明细、总收益、年化、夏普、最大回撤、胜率、权益曲线

---

## 核心路由

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/auth/register` | POST | 注册（自动创建初始资金组合） |
| `/api/auth/login` | POST | 登录（OAuth2PasswordRequestForm） |
| `/api/auth/me` | GET | 当前用户 |
| `/api/portfolio/summary` | GET | 组合摘要（实时刷新持仓市值） |
| `/api/portfolio/holdings` | GET | 持仓列表 |
| `/api/portfolio/holdings/add` | POST | 买入标的（扣现金 + 记 trade） |
| `/api/portfolio/trades` | GET | 交易记录 |
| `/api/market/quote/{symbol}` | GET | 实时行情 |
| `/api/market/history/{symbol}` | GET | K 线历史 |
| `/api/market/screener` | GET | 全景选股 |
| `/api/market/boards/concept` | GET | 概念板块 |
| `/api/market/boards/zt` | GET | 涨停池 |
| `/api/news/flash` | GET | 快讯（含逐条情绪标注 + 整体分布） |
| `/api/news/sources` | GET | 资讯源状态 |
| `/api/agent/chat` | POST | Agent 对话（主入口） |
| `/api/agent/history` | GET | 对话历史 |
| `/api/signals/market` | GET | 市场信号 |
| `/api/backtest/strategies` | GET | 策略清单 + 参数说明 |
| `/api/backtest/run` | GET | 运行回测 |
| `/api/alerts` | GET / POST | 预警列表 / 新建 |
| `/api/alerts/{id}` | PUT / DELETE | 启停 / 删除 |
| `/api/alerts/check` | POST | 手动检查一轮 |
| `/api/profile` | GET / PUT | 投资画像读写 |
| `/api/datasets` | GET / POST | 数据集列表 / 新建 |
| `/api/datasets/{id}` | GET / PUT / DELETE | 数据集详情 / 修改 / 删除 |
| `/api/datasets/{id}/publish` | POST | 发布到 RAG |
| `/api/datasets/{id}/upload` | POST | 上传文件导入样本 |
| `/api/datasets/seed` | POST | 写入内置种子数据 |

响应统一为 `{ code, msg, data }`。

---

## 配置

编辑 `.env`（或设置环境变量）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./hengce.db` | 生产换 `postgresql+asyncpg` |
| `JWT_SECRET_KEY` | `change-me...` | 生产必须替换为 32+ 位随机串 |
| `JWT_EXPIRATION_MINUTES` | `720` | Token 有效期（12h） |
| `LLM_PROVIDER` | `mock` | `mock` / `deepseek` / `openai` / `qwen` |
| `DEEPSEEK_API_KEY` | — | `LLM_PROVIDER=deepseek` 时必填 |
| `ALERT_ENABLED` | `true` | 是否启用到价预警后台轮询 |
| `ALERT_POLL_INTERVAL_SECONDS` | `60` | 轮询间隔（秒） |
| `ALERT_TRADING_HOURS_ONLY` | `true` | 仅交易时段检查（9:15-11:30 / 12:55-15:05） |
| `SMTP_HOST` / `SMTP_PORT` | — / `465` | 预警邮件推送；不配置则只做站内提醒 |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | — | 邮件账号 |
| `AGENT_CHAT_RATE_LIMIT` | `20/minute` | slowapi 限流 |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | 前端域名 |

---

## 启用真实 LLM

```bash
# 编辑 .env：
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_MODEL=deepseek-chat

# 重启后端即可，其他配置不变
```

---

## 免责声明

本平台提供的分析和建议仅供学习参考，不构成任何投资建议。投资有风险，入市需谨慎。
