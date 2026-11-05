/** 市场信号 API */
import request from './request'

export interface MarketSignalItem {
  type: string
  level: string
  message: string
}

export interface IndexAnalysis {
  index: string
  latest_close: number
  change_pct: number
  volume_ratio: number
  ma5: number
  ma20: number
  signals: MarketSignalItem[]
}

export interface MarketSignalsResult {
  scan_time: string
  indices_scanned: number
  indices_with_signals: number
  triggered: IndexAnalysis[]
  all_indices: IndexAnalysis[]
  note: string
}

export const signalsApi = {
  market() {
    return request.get<MarketSignalsResult>('/api/signals/market')
  },
}
