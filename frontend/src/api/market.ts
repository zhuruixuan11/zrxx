/** 行情 API — 扩展 A 股全景接口 */
import request from './request'
import type { MarketQuote, KlinePoint } from '@/types'

export interface ScreenerItem {
  code: string
  name: string
  market: 'SH' | 'SZ' | 'BJ'
  price: number
  change: number
  change_pct: number
  open: number
  high: number
  low: number
  prev_close: number
  volume: number
  amount: number
  timestamp: string
}

export interface ScreenerResult {
  total: number
  page: number
  size: number
  total_pages: number
  items: ScreenerItem[]
  note: string
}

export interface ConceptItem {
  label: string
  name: string
  company_count: number
  avg_price: number
  change: number
  change_pct: number
  volume: number
  amount: number
  lead_stock_code: string
  lead_stock_name: string
  lead_stock_change_pct: number
}

export interface ZtItem {
  code: string
  name: string
  change_pct: number
  price: number
  amount: number
  float_mv: number
  turnover: number
  fund_sealed: number
  first_seal_time: string
  last_seal_time: string
  broken_times: number
  consecutive: number
  industry: string
}

export const marketApi = {
  quote(symbol: string) {
    return request.get<MarketQuote>(`/api/market/quote/${symbol}`)
  },

  history(symbol: string, days = 30) {
    return request.get<{ symbol: string; period: string; data: KlinePoint[] }>(
      `/api/market/history/${symbol}`,
      { params: { days } },
    )
  },

  /** 全部 A 股实时行情（新浪源，5500+ 只） */
  screener(params: {
    page?: number
    size?: number
    sort_by?: string
    sort_order?: string
    search?: string
    market?: string
    change_min?: number
    change_max?: number
  }) {
    return request.get<ScreenerResult>('/api/market/screener', { params })
  },

  /** 腾讯概念板块（170+ 个，带涨跌幅） */
  boardsConcept() {
    return request.get<{ items: ConceptItem[] }>('/api/market/boards/concept')
  },

  /** 东方财富涨停池 */
  boardsZt() {
    return request.get<{ items: ZtItem[] }>('/api/market/boards/zt')
  },
}
