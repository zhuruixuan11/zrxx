# 衡策 · A 股智能投研平台 —— 前端

Vue 3 + TypeScript + Vite + Element Plus + ECharts 构建的 A 股投研工作台。

## 启动

```bash
npm install
npm run dev      # http://127.0.0.1:5173
npm run build    # 产物输出到 dist/
```

开发环境通过 Vite 代理把 `/api/*` 转发到后端 `http://127.0.0.1:8001`（见 `vite.config.ts`）。
启动前端前请先启动后端（见 `../backend/README.md`）。

## 页面

| 路由 | 页面 | 说明 |
|------|------|------|
| `/dashboard` | 仪表盘 | 组合概览、持仓、近期交易 |
| `/market` | A 股行情 | 个股 K 线与实时行情 |
| `/market-overview` | A 股全景 | 选股、行业/概念板块、涨停池 |
| `/news` | 资讯中心 | 快讯流 + 情绪标注 + 情绪筛选 |
| `/agent` | 衡策智算 | 对话式投研助手 |
| `/datasets` | 数据集 | 知识库样本管理、发布到 RAG |
| `/backtest` | 策略回测 | 23 套策略、参数配置、权益曲线 |
| `/alerts` | 到价预警 | 预警新建、监控中、触发记录 |
| `/profile` | 投资画像 | 风险偏好、期限、板块偏好 |

## 目录

```
src/
├── api/        # 按领域封装的接口（request.ts 统一注入 token + 解包 data）
├── views/      # 页面组件
├── layouts/    # MainLayout（侧边栏 + 内容区）
├── stores/     # Pinia（用户 token / 信息）
├── router/     # 路由 + 登录守卫
└── types/      # 共享类型定义
```

## 约定

- 后端响应统一为 `{ code, msg, data }`，`request.ts` 拦截器已解包，业务层直接拿 `data`。
- Element Plus 图标已在 `main.ts` 全局注册，无需逐个 import。
- Token 存于 `localStorage`（键名 `hengce_token` / `hengce_user`）。
