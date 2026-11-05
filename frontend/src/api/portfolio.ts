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
}
