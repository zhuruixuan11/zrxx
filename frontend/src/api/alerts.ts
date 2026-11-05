/** 到价预警 API */
import request from './request'

export type AlertOperator = '>=' | '<=' | '>' | '<'
/** price 绝对价格 / change 相对昨收涨跌幅 / cost 相对成本价涨跌幅（止盈止损） */
export type AlertType = 'price' | 'change' | 'cost'

export interface PriceAlert {
  id: string
  symbol: string
  name: string
  alert_type: AlertType
  operator: AlertOperator
  threshold: number
  reference_price?: number | null
  change_pct?: number | null
  /** 后端生成的条件文案，如「涨跌幅 >= +5.00%」 */
  condition: string
  note?: string | null
  enabled: boolean
  last_price?: number | null
  last_checked_at?: string | null
  triggered_at?: string | null
  triggered_price?: number | null
  created_at?: string | null
}

export interface MonitorInfo {
  enabled: boolean
  poll_interval_seconds: number
  trading_hours_only: boolean
  is_trading_now: boolean
  email_configured: boolean
  server_time: string
}

export interface AlertListResult {
  items: PriceAlert[]
  monitor: MonitorInfo
}

export interface CreateAlertPayload {
  symbol: string
  name?: string
  alert_type: AlertType
  operator: AlertOperator
  threshold?: number
  reference_price?: number
  change_pct?: number
  note?: string
}

export const alertsApi = {
  list() {
    return request.get<AlertListResult>('/api/alerts')
  },
  create(payload: CreateAlertPayload) {
    return request.post<PriceAlert>('/api/alerts', payload)
  },
  update(id: string, payload: { enabled?: boolean; threshold?: number; change_pct?: number }) {
    return request.put<PriceAlert>(`/api/alerts/${id}`, payload)
  },
  remove(id: string) {
    return request.delete(`/api/alerts/${id}`)
  },
  checkNow() {
    return request.post<{ checked: number; triggered: number; items: unknown[] }>('/api/alerts/check')
  },
  /** 发一封测试邮件，验证 SMTP 配置是否生效 */
  testEmail() {
    return request.post<{ sent_to: string }>('/api/alerts/test-email')
  },
}
