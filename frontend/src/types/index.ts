// 后端统一响应结构
export interface ApiResponse<T = unknown> {
  code: number
  msg: string
  data: T
}

// 用户
export interface User {
  id: string
  username: string
  email: string
  risk_level: string
  bio?: string
}

// 投资组合概览
export interface PortfolioSummary {
  portfolio_id: string
  name: string
  total_value: number
  cash_balance: number
  total_cost: number
  total_pnl: number
  pnl_percent: number
  initial_capital: number
}

// 持仓
export interface Holding {
  id: string
  symbol: string
  asset_type: string
  quantity: number
  avg_cost: number
  current_price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pnl_percent: number
}

// 交易
export interface Trade {
  id: string
  symbol: string
  action: string
  status: string
  quantity: number
  price: number
  total_amount: number
  trade_time: string
}

// 行情
export interface MarketQuote {
  symbol: string
  bs_code?: string        // baostock 代码 sh.600519
  trade_date?: string     // 最近交易日
  current_price: number
  previous_close: number
  change: number
  change_percent: number
  open: number
  high: number
  low: number
  volume: number
  amount?: number         // 成交额（A 股）
  data_source: string     // baostock / baostock+eastmoney / yfinance
  is_realtime?: boolean
}

// K 线点
export interface KlinePoint {
  date: string
  open: number
  close: number
  high: number
  low: number
  volume: number
}

// Agent 对话
export interface AgentChatRequest {
  message: string
}

export interface AgentChatResponse {
  response: string
  intent: string
  response_type: string
  sources?: Array<{ title: string; content: string }>
}

// 聊天历史
export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  intent?: string
  sources?: string[]
  created_at: string
}
