/** 持仓跟随监控 API */
import request from './request'

export type WatchEventType = 'price' | 'holding' | 'announcement'

export interface WatchItem {
  id: string
  symbol: string
  name: string
  price_threshold_pct: number
  watch_price: boolean
  watch_holding: boolean
  watch_announcement: boolean
  baseline_quantity: number | null
  baseline_at: string | null
  enabled: boolean
  last_price: number | null
  last_checked_at: string | null
  last_announcement_at: string | null
  created_at: string | null
}

export interface WatchEvent {
  id: string
  watch_id: string
  symbol: string
  event_type: WatchEventType
  title: string
  detail: string | null
  notified: boolean
  created_at: string | null
}

export interface WatchMonitor {
  enabled: boolean
  poll_interval_seconds: number
  is_trading_now: boolean
  email_configured: boolean
  server_time: string
}

export interface WatchOverview {
  items: WatchItem[]
  events: WatchEvent[]
  monitor: WatchMonitor
}

export interface WatchCheckResult {
  checked: number
  created: number
  by_type: Record<string, number>
  events: Array<{
    symbol: string
    event_type: WatchEventType
    title: string
    detail: string
    created_at: string
  }>
  email_configured: boolean
}

export interface WatchCreatePayload {
  symbol: string
  name?: string
  price_threshold_pct?: number
  watch_price?: boolean
  watch_holding?: boolean
  watch_announcement?: boolean
}

export const watchApi = {
  list() {
    return request.get<WatchOverview>('/api/watch')
  },
  create(payload: WatchCreatePayload) {
    return request.post<WatchItem>('/api/watch', payload)
  },
  update(id: string, payload: Partial<Omit<WatchItem, 'id'>>) {
    return request.put<WatchItem>(`/api/watch/${id}`, payload)
  },
  remove(id: string) {
    return request.delete(`/api/watch/${id}`)
  },
  check(force = true) {
    return request.post<WatchCheckResult>('/api/watch/check', null, { params: { force } })
  },
  events(limit = 50) {
    return request.get<{ items: WatchEvent[] }>('/api/watch/events', { params: { limit } })
  },
}
