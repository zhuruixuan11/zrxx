/** 投资组合 API */
import request from './request'
import type { PortfolioSummary, Holding, Trade } from '@/types'

export const portfolioApi = {
  summary() {
    return request.get<PortfolioSummary>('/api/portfolio/summary')
  },

  holdings() {
    return request.get<{ list: Holding[]; total: number }>('/api/portfolio/holdings')
  },

  trades() {
    return request.get<{ list: Trade[]; total: number }>('/api/portfolio/trades')
  },

  /** 从回测结果一键开模拟仓（取 trades 里首笔 BUY） */
  fromBacktest(payload: {
    symbol: string
    strategy: string
    trades: Array<{ action: string; price: number; shares: number; [key: string]: any }>
    initial_capital?: number
    notes?: string
  }) {
    return request.post<{
      symbol: string
      shares: number
      price: number
      total_amount: number
      portfolio_cash: number
      notes: string
    }>('/api/portfolio/from-backtest', payload)
  },
}
