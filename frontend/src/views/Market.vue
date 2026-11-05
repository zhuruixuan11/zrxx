<template>
  <div class="market-page">
    <!-- 查询区 -->
    <el-card shadow="hover">
      <div class="search-row">
        <el-input
          v-model="symbol"
          placeholder="输入 A 股代码，如 600519（茅台）/ 000001（平安）/ 300750（宁德）"
          @keyup.enter="fetchQuote"
          style="width: 380px"
        >
          <template #append>
            <el-button :icon="Search" @click="fetchQuote">查询</el-button>
          </template>
        </el-input>
        <el-button
          v-for="s in hotStocks"
          :key="s.code"
          size="small"
          :type="symbol === s.code ? 'primary' : ''"
          @click="quickSearch(s.code)"
        >
          {{ s.name }}({{ s.code }})
        </el-button>
      </div>
      <div v-if="quote" class="data-source">
        数据源:
        <el-tag size="small" :type="quote.is_realtime ? 'success' : 'info'">
          {{ quote.data_source }}
        </el-tag>
        <span class="trade-date">交易日: {{ quote.trade_date }}</span>
      </div>
    </el-card>

    <!-- 行情展示 -->
    <template v-if="quote">
      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :xs="24" :md="8">
          <el-card shadow="hover" class="quote-card" :class="quote.change >= 0 ? 'up' : 'down'">
            <div class="symbol-header">
              <h2>{{ quote.symbol }}</h2>
              <el-tag size="small">{{ quote.bs_code }}</el-tag>
            </div>
            <div class="price-row">
              <span class="price num">¥{{ quote.current_price?.toFixed(2) }}</span>
              <span class="change num">
                {{ quote.change >= 0 ? '+' : '' }}{{ quote.change?.toFixed(2) }}
                ({{ quote.change_percent >= 0 ? '+' : '' }}{{ quote.change_percent?.toFixed(2) }}%)
              </span>
            </div>
            <div class="quote-detail">
              <div><span>今开</span>¥{{ quote.open?.toFixed(2) }}</div>
              <div><span>最高</span>¥{{ quote.high?.toFixed(2) }}</div>
              <div><span>最低</span>¥{{ quote.low?.toFixed(2) }}</div>
              <div><span>昨收</span>¥{{ quote.previous_close?.toFixed(2) }}</div>
              <div><span>成交量</span>{{ (quote.volume / 10000).toFixed(0) }}万</div>
              <div><span>成交额</span>{{ formatAmount(quote.amount) }}</div>
            </div>
          </el-card>
        </el-col>

        <!-- K 线图 -->
        <el-col :xs="24" :md="16">
          <el-card shadow="hover">
            <template #header>
              <div style="display: flex; justify-content: space-between; align-items: center">
                <span style="font-weight: 600">历史走势（baostock 日线）</span>
                <el-radio-group v-model="range" size="small" @change="fetchHistory">
                  <el-radio-button :value="7">7天</el-radio-button>
                  <el-radio-button :value="30">30天</el-radio-button>
                  <el-radio-button :value="90">90天</el-radio-button>
                  <el-radio-button :value="250">1年</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <v-chart v-if="history.length" :option="klineOption" theme="hengce" style="height: 350px" autoresize v-loading="histLoading" />
            <div v-else v-loading="histLoading" style="height: 350px; display: flex; align-items: center; justify-content: center">
              <el-empty description="暂无 K 线数据，数据源暂时不可用，请稍后重试" :image-size="80" />
            </div>
          </el-card>
        </el-col>
      </el-row>
    </template>

    <el-empty v-else-if="!loading" description="输入 A 股 6 位代码开始查询（baostock 真实数据）" style="margin-top: 60px" />
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { marketApi } from '@/api/market'
import type { MarketQuote, KlinePoint } from '@/types'

const symbol = ref('600519')
const range = ref(30)
const loading = ref(false)
const histLoading = ref(false)
const quote = ref<MarketQuote | null>(null)
const history = ref<KlinePoint[]>([])

const hotStocks = [
  { code: '600519', name: '贵州茅台' },
  { code: '000001', name: '平安银行' },
  { code: '300750', name: '宁德时代' },
  { code: '601318', name: '中国平安' },
  { code: '000858', name: '五粮液' },
]

async function fetchQuote() {
  const sym = symbol.value.trim()
  if (!sym) return
  loading.value = true
  try {
    quote.value = await marketApi.quote(sym)
    await fetchHistory()
  } catch { /* request interceptor handles error toast */ }
  finally {
    loading.value = false
  }
}

async function fetchHistory() {
  const sym = symbol.value.trim()
  if (!sym) return
  histLoading.value = true
  try {
    const data = await marketApi.history(sym, range.value)
    history.value = data.data || []
  } catch { /* ignore */ }
  finally {
    histLoading.value = false
  }
}

function quickSearch(code: string) {
  symbol.value = code
  fetchQuote()
}

// K 线图
const klineOption = ref<any>({ tooltip: {}, grid: {}, xAxis: { data: [] }, series: [] })

watch(history, (data) => {
  if (!data.length) return
  const first = data[0].close
  const last = data[data.length - 1].close
  const color = last >= first ? '#e74c3c' : '#27ae60'  // 红涨绿跌（A股习惯）
  klineOption.value = {
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: data.map((d) => d.date.slice(5)), boundaryGap: false },
    yAxis: { type: 'value', scale: true },
    series: [{
      name: '收盘', type: 'line', smooth: true,
      data: data.map((d) => d.close),
      areaStyle: { opacity: 0.15, color },
      lineStyle: { width: 2, color },
      itemStyle: { color },
    }],
  }
}, { deep: true })

function formatAmount(amount: number) {
  if (!amount) return '0'
  if (amount >= 1e8) return (amount / 1e8).toFixed(2) + '亿'
  if (amount >= 1e4) return (amount / 1e4).toFixed(0) + '万'
  return amount.toFixed(0)
}

// 默认查茅台
fetchQuote()
</script>

<style scoped>
.search-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.data-source {
  margin-top: 12px;
  font-size: 12px;
  color: #606266;
  display: flex;
  align-items: center;
  gap: 8px;
}
.trade-date {
  color: #909399;
}
.quote-card {
  color: #fff;
  border: none;
}
.quote-card.up { background: linear-gradient(135deg, #d64541 0%, #a52f2c 100%); }
.quote-card.down { background: linear-gradient(135deg, #2e8b74 0%, #1f6b57 100%); }
.quote-card :deep(.el-card__body) { padding: 24px; }
.symbol-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.symbol-header h2 { font-size: 28px; margin: 0; color: #fff; }
.price-row { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
.price { font-size: 36px; font-weight: 700; }
.change { font-size: 16px; padding: 2px 8px; border-radius: 4px; background: rgba(255, 255, 255, 0.2); }
.quote-detail {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
  font-size: 13px;
}
.quote-detail span { opacity: 0.7; margin-right: 6px; }
</style>
