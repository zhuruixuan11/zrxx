<template>
  <div class="news-page">
    <!-- 市场信号卡 -->
    <el-card shadow="hover" class="signals-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600; font-size: 16px">
            <el-icon style="margin-right: 6px"><Warning /></el-icon>
            大盘异动信号
          </span>
          <div style="display: flex; gap: 8px; align-items: center">
            <span v-if="signalsResult?.scan_time" class="fetched-time">扫描于 {{ signalsResult.scan_time }}</span>
            <el-button :icon="Refresh" size="small" :loading="signalsLoading" @click="loadSignals">刷新</el-button>
          </div>
        </div>
      </template>

      <div v-if="signalsLoading" style="padding: 20px; text-align: center; color: #909399">
        <el-icon class="is-loading" :size="24"><Loading /></el-icon>
        <span style="margin-left: 8px">正在扫描大盘指数（baostock 首次连接较慢）...</span>
      </div>

      <template v-else-if="signalsResult">
        <!-- 触发的信号 -->
        <div v-if="signalsResult.triggered?.length" class="triggered-list">
          <el-alert
            v-for="t in signalsResult.triggered"
            :key="t.index"
            :title="`${t.index} · 最新 ${t.latest_close} · ${t.change_pct >= 0 ? '+' : ''}${t.change_pct.toFixed(2)}%`"
            :type="t.change_pct >= 0 ? 'error' : 'success'"
            show-icon
            :closable="false"
            style="margin-bottom: 10px"
          >
            <div v-for="(s, idx) in t.signals" :key="idx" class="signal-item">
              <el-icon v-if="s.type === 'sharp_move' && t.change_pct < 0"><CaretTop /></el-icon>
              <el-icon v-else-if="s.type === 'sharp_move'"><CaretBottom /></el-icon>
              <el-icon v-else><InfoFilled /></el-icon>
              <span>{{ s.message.replace(/^[📈🔊✨⚠️]\s*/, '') }}</span>
            </div>
          </el-alert>
        </div>

        <!-- 无信号时展示所有指数概览 -->
        <el-row v-else :gutter="10" class="indices-grid">
          <el-col
            v-for="idx in signalsResult.all_indices"
            :key="idx.index"
            :xs="12" :sm="8" :md="6" :lg="4"
          >
            <div class="index-tile" :class="{ 'tile-up': idx.change_pct >= 0, 'tile-down': idx.change_pct < 0 }">
              <div class="index-name">{{ idx.index }}</div>
              <div class="index-price">{{ idx.latest_close.toFixed(2) }}</div>
              <div class="index-change" :class="idx.change_pct >= 0 ? 'num-positive' : 'num-negative'">
                {{ idx.change_pct >= 0 ? '+' : '' }}{{ idx.change_pct.toFixed(2) }}%
              </div>
              <div class="index-sub">量比 {{ idx.volume_ratio.toFixed(1) }}x</div>
            </div>
          </el-col>
        </el-row>
      </template>

      <el-empty v-else description="大盘指数扫描失败，稍后重试" />
    </el-card>

    <!-- 新闻列表卡 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600; font-size: 16px">
            <el-icon style="margin-right: 6px"><Reading /></el-icon>
            A 股财经快讯
          </span>
          <div style="display: flex; gap: 8px; align-items: center">
            <span v-if="result?.fetched_at" class="fetched-time">更新于 {{ result.fetched_at }}</span>
            <el-button :icon="Refresh" size="small" :loading="loading" @click="loadNews">刷新</el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="result?.items && result.items.length === 0 && result.note"
        :title="result.note"
        type="warning"
        show-icon
        :closable="false"
        style="margin-bottom: 16px"
      />

      <!-- 情绪汇总 -->
      <div v-if="result?.sentiment_summary?.total" class="sentiment-bar">
        <div class="sentiment-head">
          <span class="sentiment-label" :class="`sent-${result.sentiment_summary.sentiment}`">
            {{ result.sentiment_summary.label }}
          </span>
          <span class="sentiment-note">
            近 {{ result.sentiment_summary.total }} 条快讯 ·
            利好 {{ result.sentiment_summary.bullish }} ·
            利空 {{ result.sentiment_summary.bearish }} ·
            中性 {{ result.sentiment_summary.neutral }}
          </span>
        </div>
        <div class="sentiment-track">
          <div class="bar bull" :style="{ width: bullPct + '%' }" />
          <div class="bar neutral" :style="{ width: neutralPct + '%' }" />
          <div class="bar bear" :style="{ width: bearPct + '%' }" />
        </div>
        <el-radio-group v-model="filter" size="small" style="margin-top: 10px">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="bullish">利好</el-radio-button>
          <el-radio-button value="bearish">利空</el-radio-button>
          <el-radio-button value="neutral">中性</el-radio-button>
        </el-radio-group>
      </div>

      <div v-if="filteredItems.length" class="news-list" v-loading="loading">
        <div v-for="(it, idx) in filteredItems" :key="idx" class="news-item">
          <div class="news-tag">
            <el-tag size="small" :type="tagColor(it.source)">{{ it.source }}</el-tag>
            <el-tag
              v-if="it.sentiment"
              size="small"
              :type="sentimentTagType(it.sentiment)"
              effect="dark"
              style="margin-top: 6px"
            >{{ sentimentLabel(it.sentiment) }}</el-tag>
          </div>
          <div class="news-body">
            <a
              v-if="it.url"
              :href="it.url"
              target="_blank"
              rel="noopener"
              class="news-title"
            >{{ it.title }}</a>
            <span v-else class="news-title">{{ it.title }}</span>
            <p v-if="it.summary" class="news-summary">{{ it.summary }}</p>
            <div class="news-meta">
              <span class="news-time">{{ it.publish_time_str }}</span>
              <span v-if="it.sentiment_keywords?.length" class="news-keywords">
                命中：{{ it.sentiment_keywords.join('、') }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <el-empty
        v-else-if="!loading"
        :description="filter === 'all' ? '暂无可用新闻（不返回虚假新闻）' : '当前筛选下没有匹配的快讯'"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { newsApi, type NewsFlashResult, type Sentiment } from '@/api/news'
import { signalsApi, type MarketSignalsResult } from '@/api/signals'

const loading = ref(false)
const result = ref<NewsFlashResult | null>(null)
const signalsLoading = ref(false)
const signalsResult = ref<MarketSignalsResult | null>(null)
const filter = ref<'all' | Sentiment>('all')

const filteredItems = computed(() => {
  const items = result.value?.items || []
  return filter.value === 'all' ? items : items.filter(i => i.sentiment === filter.value)
})

const bullPct = computed(() => {
  const s = result.value?.sentiment_summary
  return s?.total ? (s.bullish / s.total) * 100 : 0
})
const bearPct = computed(() => {
  const s = result.value?.sentiment_summary
  return s?.total ? (s.bearish / s.total) * 100 : 0
})
const neutralPct = computed(() => {
  const s = result.value?.sentiment_summary
  return s?.total ? (s.neutral / s.total) * 100 : 0
})

/** A 股习惯：红涨绿跌，所以利好用红、利空用绿 */
function sentimentTagType(s: Sentiment): 'danger' | 'success' | 'info' {
  return s === 'bullish' ? 'danger' : s === 'bearish' ? 'success' : 'info'
}
function sentimentLabel(s: Sentiment): string {
  return s === 'bullish' ? '利好' : s === 'bearish' ? '利空' : '中性'
}

async function loadNews() {
  loading.value = true
  try {
    result.value = await newsApi.flash(50)
  } catch { /* interceptor handles */ }
  finally { loading.value = false }
}

async function loadSignals() {
  signalsLoading.value = true
  try {
    signalsResult.value = await signalsApi.market()
  } catch {
    signalsResult.value = null
  } finally { signalsLoading.value = false }
}

function tagColor(source: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  const m: Record<string, '' | 'success' | 'warning' | 'info' | 'danger'> = {
    '东方财富': '',
    '财联社': 'warning',
    '钛媒体': 'success',
    '36氪': 'info',
  }
  return m[source] || 'info'
}

onMounted(() => {
  loadNews()
  loadSignals()
})
setInterval(loadNews, 60_000)
// 信号扫描耗时长（baostock login ~20s），5 分钟刷新一次
setInterval(loadSignals, 300_000)
</script>

<style scoped>
.fetched-time {
  font-size: 12px;
  color: #909399;
}
.news-list {
  max-height: calc(100vh - 480px);
  overflow-y: auto;
}
/* 情绪汇总 */
.sentiment-bar {
  padding: 12px 14px;
  border-radius: 8px;
  background: #fafbfc;
  border: 1px solid #ebeef5;
  margin-bottom: 12px;
}
.sentiment-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.sentiment-label {
  font-weight: 600;
  font-size: 15px;
}
.sent-bullish { color: #e74c3c; }
.sent-bearish { color: #27ae60; }
.sent-neutral { color: #909399; }
.sentiment-note {
  font-size: 12px;
  color: #909399;
}
.sentiment-track {
  display: flex;
  height: 6px;
  border-radius: 3px;
  overflow: hidden;
  background: #f0f2f5;
}
.sentiment-track .bar { height: 100%; }
.sentiment-track .bull { background: #e74c3c; }
.sentiment-track .neutral { background: #dcdfe6; }
.sentiment-track .bear { background: #27ae60; }
.news-item {
  display: flex;
  gap: 12px;
  padding: 14px 0;
  border-bottom: 1px solid #ebeef5;
}
.news-item:last-child { border-bottom: none; }
.news-tag { flex-shrink: 0; margin-top: 2px; display: flex; flex-direction: column; }
.news-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}
.news-keywords {
  font-size: 11px;
  color: #b0b3b8;
}
.news-body { flex: 1; min-width: 0; }
.news-title {
  font-size: 15px;
  font-weight: 500;
  color: #303133;
  line-height: 1.5;
  text-decoration: none;
  display: block;
}
.news-title:hover {
  color: var(--el-color-primary);
  text-decoration: underline;
}
.news-summary {
  font-size: 13px;
  color: #909399;
  margin: 6px 0;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.news-time {
  font-size: 12px;
  color: #c0c4cc;
}

/* 信号区 */
.triggered-list { margin-bottom: 4px; }
.signal-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  margin-top: 4px;
}
.indices-grid { margin-top: 4px; }
.index-tile {
  padding: 12px;
  border-radius: 8px;
  text-align: center;
  border: 1px solid #ebeef5;
  transition: transform 0.15s;
  margin-bottom: 10px;
}
.index-tile:hover { transform: translateY(-2px); }
.index-name { font-size: 13px; color: #606266; font-weight: 500; margin-bottom: 4px; }
.index-price { font-size: 18px; font-weight: 600; color: #303133; }
.index-change { font-size: 14px; font-weight: 500; margin: 2px 0; }
.index-sub { font-size: 11px; color: #909399; }
.num-positive { color: #e74c3c; }
.num-negative { color: #27ae60; }
</style>
