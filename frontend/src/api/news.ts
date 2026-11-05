/** 新闻 API */
import request from './request'

export type Sentiment = 'bullish' | 'bearish' | 'neutral'

export interface NewsItem {
  title: string
  summary: string
  url: string
  source: string
  publish_time: number
  publish_time_str: string
  sentiment?: Sentiment
  sentiment_score?: number
  sentiment_keywords?: string[]
}

export interface SentimentSummary {
  bullish: number
  bearish: number
  neutral: number
  total: number
  score: number
  sentiment: Sentiment
  label: string
}

export interface NewsFlashResult {
  items: NewsItem[]
  total: number
  fetched_at: string
  sentiment_summary?: SentimentSummary
  note?: string
}

export const newsApi = {
  flash(limit = 30) {
    return request.get<NewsFlashResult>('/api/news/flash', { params: { limit } })
  },
}
