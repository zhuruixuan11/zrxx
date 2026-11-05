<template>
  <div class="dashboard">
    <!-- 概览卡片 -->
    <el-row :gutter="16" class="cards">
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-label">总资产</div>
          <div class="stat-value num">¥{{ fmt(summary.total_value) }}</div>
          <div class="stat-sub">初始 ¥{{ fmt(summary.initial_capital) }}</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-label">可用现金</div>
          <div class="stat-value num">¥{{ fmt(summary.cash_balance) }}</div>
          <div class="stat-sub">占比 {{ cashRatio }}%</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-label">累计盈亏</div>
          <div class="stat-value num" :class="pnlClass">
            {{ summary.total_pnl >= 0 ? '+' : '' }}¥{{ fmt(summary.total_pnl) }}
          </div>
          <div class="stat-sub" :class="pnlClass">
            {{ summary.pnl_percent >= 0 ? '+' : '' }}{{ summary.pnl_percent?.toFixed(2) }}%
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-label">持仓数量</div>
          <div class="stat-value">{{ holdingsList.length }}</div>
          <div class="stat-sub">{{ summary.name }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 下方区域：饼图 + 持仓表 -->
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :xs="24" :md="8">
        <el-card shadow="hover">
          <template #header>
            <span style="font-weight: 600">资产分布</span>
          </template>
          <v-chart :option="pieOption" theme="hengce" style="height: 300px" autoresize />
        </el-card>
      </el-col>
      <el-col :xs="24" :md="16">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">持仓明细</span>
              <el-button :icon="Refresh" circle size="small" @click="loadData" />
            </div>
          </template>
          <el-table :data="holdingsList" stripe style="width: 100%" v-loading="loading">
            <el-table-column prop="symbol" label="代码" width="110" />
            <el-table-column prop="asset_type" label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="assetTagType(row.asset_type)">{{ row.asset_type }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="quantity" label="数量" width="90" class-name="num">
              <template #default="{ row }">{{ row.quantity?.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="成本价" width="100" class-name="num">
              <template #default="{ row }">¥{{ row.avg_cost?.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="现价" width="100" class-name="num">
              <template #default="{ row }">¥{{ row.current_price?.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="市值" width="110" class-name="num">
              <template #default="{ row }">¥{{ fmt(row.market_value) }}</template>
            </el-table-column>
            <el-table-column label="盈亏" width="110" class-name="num">
              <template #default="{ row }">
                <span :class="row.unrealized_pnl >= 0 ? 'num-positive' : 'num-negative'">
                  {{ row.unrealized_pnl >= 0 ? '+' : '' }}¥{{ fmt(row.unrealized_pnl) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="盈亏%" width="90" class-name="num">
              <template #default="{ row }">
                <span :class="row.unrealized_pnl_percent >= 0 ? 'num-positive' : 'num-negative'">
                  {{ row.unrealized_pnl_percent >= 0 ? '+' : '' }}{{ row.unrealized_pnl_percent?.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="quoteTagColor(row.quote_source)">
                  {{ row.quote_source || '—' }}
                </el-tag>
              </template>
            </el-table-column>
            <template #empty>
              <div style="padding: 20px; color: #909399">暂无持仓，去行情页面买入第一只标的吧！</div>
            </template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { portfolioApi } from '@/api/portfolio'
import type { PortfolioSummary, Holding } from '@/types'

const loading = ref(false)
const summary = ref<PortfolioSummary>({
  portfolio_id: '', name: '我的投资组合',
  total_value: 0, cash_balance: 0, total_cost: 0,
  total_pnl: 0, pnl_percent: 0, initial_capital: 100000,
})
const holdingsList = ref<Holding[]>([])

async function loadData() {
  loading.value = true
  try {
    const [s, h] = await Promise.all([
      portfolioApi.summary(),
      portfolioApi.holdings(),
    ])
    summary.value = s
    holdingsList.value = h?.list || []
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

// 派生数据
const cashRatio = computed(() => {
  if (!summary.value.total_value) return 100
  return ((summary.value.cash_balance / summary.value.total_value) * 100).toFixed(1)
})
const pnlClass = computed(() =>
  summary.value.total_pnl >= 0 ? 'num-positive' : 'num-negative',
)

// 饼图数据
const pieOption = computed(() => {
  const series: { name: string; value: number }[] = []
  const s = summary.value
  if (s.cash_balance > 0) series.push({ name: '现金', value: s.cash_balance })
  for (const h of holdingsList.value) {
    if (h.market_value > 0) series.push({ name: h.symbol, value: h.market_value })
  }
  if (series.length === 0) series.push({ name: '暂无资产', value: 1 })

  return {
    tooltip: { trigger: 'item', formatter: '{b}: ¥{c} ({d}%)' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { show: true, formatter: '{b}\n{d}%' },
      data: series,
    }],
  }
})

// 工具函数
function fmt(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '0.00'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function assetTagType(t: string) {
  const map: Record<string, string> = { stock: '', etf: 'success', bond: 'warning', cash: 'info' }
  return map[t] || ''
}
function quoteTagColor(src: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (!src) return 'info'
  if (src.includes('eastmoney') && src.includes('baostock')) return 'success'
  if (src.includes('baostock')) return ''
  if (src.includes('yfinance')) return 'warning'
  return 'info'
}
</script>

<style scoped>
.stat-card {
  border-radius: 8px;
}
.stat-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 6px;
}
.stat-value {
  font-size: 26px;
  font-weight: 600;
  color: #303133;
}
.stat-sub {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.cards {
  margin-bottom: 16px;
}
</style>
