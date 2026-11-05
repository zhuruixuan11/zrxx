/** 策略回测 API */
import request from './request'

export interface BacktestMetrics {
  total_return_pct: number
  annual_return_pct: number
  sharpe_ratio: number
  max_drawdown_pct: number
  win_rate_pct: number
  initial_capital: number
  final_equity: number
  trades_count: number
}

export interface BacktestTrade {
  date: string
  action: 'BUY' | 'SELL'
  price: number
  shares: number
  amount: number
  fee: number
  pnl: number
}

export interface EquityPoint {
  date: string
  equity: number
}

export interface BacktestResult {
  symbol: string
  strategy: string
  strategy_name: string
  params: Record<string, unknown>
  bars_count: number
  warning?: string | null
  metrics: BacktestMetrics
  trades: BacktestTrade[]
  equity_curve: EquityPoint[]
}

export interface StrategyInfo {
  id: string
  name: string
  group: string
  desc: string
  disabled: boolean
  params: Array<{ name: string; default: number; desc: string }>
}

export interface StrategiesResult {
  strategies: StrategyInfo[]
  groups: string[]
  note: string
}

/** 回测历史记录（列表不含权益曲线） */
export interface BacktestHistoryItem {
  id: string
  symbol: string
  strategy: string
  strategy_name: string
  params: Record<string, unknown>
  initial_capital: number
  days: number
  metrics: BacktestMetrics
  created_at: string | null
}

/** 对比用的历史记录（含权益曲线） */
export interface BacktestHistoryDetail extends BacktestHistoryItem {
  equity_curve: EquityPoint[]
}

export const backtestApi = {
  run(payload: {
    symbol: string
    strategy: string
    initial_capital?: number
    days?: number
    params?: Record<string, unknown>
  }) {
    return request.get<BacktestResult>('/api/backtest/run', {
      params: {
        symbol: payload.symbol,
        strategy: payload.strategy,
        initial_capital: payload.initial_capital,
        days: payload.days,
        params: JSON.stringify(payload.params || {}),
      },
    })
  },

  strategies() {
    return request.get<StrategiesResult>('/api/backtest/strategies')
  },

  /** 保存本次回测结果到历史 */
  save(result: BacktestResult, days: number) {
    return request.post<BacktestHistoryItem>('/api/backtest/save', {
      symbol: result.symbol,
      strategy: result.strategy,
      initial_capital: result.metrics.initial_capital,
      days,
      params: result.params,
      metrics: result.metrics,
      equity_curve: result.equity_curve,
    })
  },

  history(limit = 30) {
    return request.get<{ items: BacktestHistoryItem[] }>('/api/backtest/history', {
      params: { limit },
    })
  },

  remove(id: string) {
    return request.delete(`/api/backtest/history/${id}`)
  },

  /** 多策略对比（2-3 条） */
  compare(ids: string[]) {
    return request.get<{ items: BacktestHistoryDetail[] }>('/api/backtest/compare', {
      params: { ids: ids.join(',') },
    })
  },
}
