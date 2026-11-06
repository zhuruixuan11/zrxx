/** Agent 对话 API */
import request from './request'
import type { AgentChatResponse } from '@/types'

export interface ChatHistoryItem {
  id: string
  role: 'user' | 'assistant'
  content: string
  intent?: string
  created_at?: string
  /** 服务端持久化的回测卡片数据（有卡片的历史消息才带） */
  symbols?: string[]
  symbol_names?: string[]
  backtest_results?: Array<{
    symbol: string
    symbol_name: string
    results: Array<{
      strategy_id: string
      strategy_name: string
      metrics: {
        total_return_pct: number
        annual_return_pct: number
        sharpe_ratio: number
        max_drawdown_pct: number
        win_rate_pct: number
        trades_count: number
      }
    }>
  }>
}

export const agentApi = {
  chat(message: string) {
    // 分析类问题需要拉行情+新闻，耗时较长，单独放宽超时（后端自身兜底 75s）
    return request.post<AgentChatResponse>('/api/agent/chat', { message }, { timeout: 90000 })
  },
  /** 服务端对话记录（换设备/清缓存后仍可恢复） */
  history(limit = 40) {
    return request.get<{ list: ChatHistoryItem[]; total: number }>('/api/agent/history', {
      params: { limit },
    })
  },
  clearHistory() {
    return request.delete<{ deleted: number }>('/api/agent/history')
  },
}
