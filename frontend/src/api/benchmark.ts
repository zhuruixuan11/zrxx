/** 基准跟随（跟踪误差分析）API */
import request from './request'

export type BenchmarkId = 'hs300' | 'zz500' | 'custom'
export type BenchmarkSource = 'strategy' | 'run' | 'portfolio'

export interface BenchmarkOption {
  id: BenchmarkId
  name: string
  code: string | null
  available: boolean
  desc: string
}

export interface BenchmarkOptions {
  benchmarks: BenchmarkOption[]
  holdings_count: number
  formulas: Record<string, string>
  data_source_note: string
}

export interface BenchmarkSeriesPoint {
  date: string
  strategy_nav: number
  benchmark_nav: number
  excess_ratio: number
  excess_pct: number
}

export interface BenchmarkMetrics {
  strategy: {
    cumulative_return_pct: number
    annualized_return_pct: number
    max_drawdown_pct: number
    final_nav: number
  }
  benchmark: {
    cumulative_return_pct: number
    annualized_return_pct: number
    max_drawdown_pct: number
    final_nav: number
  }
  excess: {
    cumulative_pct: number
    annualized_pct: number
    tracking_error_pct: number
    beta: number
    alpha_pct: number
  }
  trading_days: number
  risk_free_rate: number
}

export interface IndustryRow {
  industry: string
  weight_pct: number
  benchmark_weight_pct: number | null
  deviation_pct: number | null
}

export interface IndustrySection {
  available: boolean
  benchmark_available: boolean
  items: IndustryRow[]
  note: string
}

export interface BenchmarkAnalysis {
  strategy: {
    name: string
    symbol: string
    strategy: string
    initial_capital: number
  }
  benchmark: {
    id: BenchmarkId
    name: string
    code: string | null
    source: string
  }
  period: { start: string; end: string; trading_days: number }
  series: BenchmarkSeriesPoint[]
  metrics: BenchmarkMetrics
  industry: IndustrySection
  data_sources: Record<string, string>
  formulas: Record<string, string>
  notes: string[]
}

export interface AnalyzePayload {
  source: BenchmarkSource
  benchmark: BenchmarkId
  symbol?: string
  strategy?: string
  params?: Record<string, number>
  initial_capital?: number
  days?: number
  run_id?: string
}

export const benchmarkApi = {
  options() {
    return request.get<BenchmarkOptions>('/api/benchmark/options')
  },
  analyze(payload: AnalyzePayload) {
    return request.post<BenchmarkAnalysis>('/api/benchmark/analyze', payload, { timeout: 120000 })
  },
}
