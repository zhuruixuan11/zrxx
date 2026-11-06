<template>
  <div class="backtest-page">
    <!-- 表单卡片 -->
    <el-card shadow="hover">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600; font-size: 16px">
            <el-icon style="margin-right: 6px"><DataAnalysis /></el-icon>
            策略回测
          </span>
          <el-tag type="info" size="small">数据源：baostock 真实日线（前复权）</el-tag>
        </div>
      </template>

      <el-form :model="form" label-width="110px" inline>
        <el-form-item label="股票代码">
          <el-input
            v-model="form.symbol"
            placeholder="如 600519 / 000001"
            style="width: 160px"
            maxlength="6"
          />
        </el-form-item>
        <el-form-item label="策略">
          <el-select v-model="form.strategy" style="width: 240px" @change="onStrategyChange">
            <el-option-group
              v-for="g in groups"
              :key="g"
              :label="g"
            >
              <el-option
                v-for="s in strategiesOf(g)"
                :key="s.id"
                :label="s.name"
                :value="s.id"
                :disabled="s.disabled"
              />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="初始资金">
          <el-input-number v-model="form.initial_capital" :min="1000" :step="10000" :precision="0" />
        </el-form-item>
        <el-form-item label="回测天数">
          <el-input-number v-model="form.days" :min="60" :max="500" :step="30" />
        </el-form-item>
        <el-form-item
          v-for="p in currentParams"
          :key="p.name"
          :label="p.desc"
        >
          <el-input-number v-model="form.params[p.name]" :step="p.default >= 1 ? 1 : 0.1" :min="0" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="running" @click="runBacktest">
            <el-icon><Promotion /></el-icon>
            运行回测
          </el-button>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="currentStrategy"
        :title="`${currentStrategy.name}：${currentStrategy.desc}`"
        type="info"
        :closable="false"
        show-icon
        style="margin-top: 4px"
      />

      <div class="fee-note">
        <el-icon><InfoFilled /></el-icon>
        费用模型：佣金万三 + 卖出印花税千一，次日开盘成交
      </div>
    </el-card>

    <!-- 结果区域 -->
    <template v-if="result">
      <el-alert
        v-if="result.warning"
        :title="result.warning"
        type="warning"
        :closable="false"
        show-icon
        style="margin-top: 16px"
      />
      <div class="result-bar">
        <span class="result-summary">
          <el-tag type="success" size="small">回测完成</el-tag>
          <span class="muted-text">
            {{ result.strategy_name }} · {{ result.symbol }} · {{ result.metrics.trades_count }} 笔交易
          </span>
        </span>
        <el-button size="small" type="primary" :loading="saving" @click="saveCurrent">
          <el-icon><CollectionTag /></el-icon>
          保存到历史
        </el-button>
        <el-button
          size="small"
          type="success"
          :loading="openingPortfolio"
          :disabled="!hasBuySignal"
          @click="openFromBacktest"
        >
          <el-icon><Briefcase /></el-icon>
          一键开模拟仓
        </el-button>
      </div>
      <!-- 指标卡 -->
      <el-row :gutter="16" class="cards" style="margin-top: 16px">
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">总收益率</div>
            <div class="metric-value" :class="cls(result.metrics.total_return_pct)">
              {{ pct(result.metrics.total_return_pct) }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">年化收益率</div>
            <div class="metric-value" :class="cls(result.metrics.annual_return_pct)">
              {{ pct(result.metrics.annual_return_pct) }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">夏普比率</div>
            <div class="metric-value">{{ result.metrics.sharpe_ratio.toFixed(2) }}</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">最大回撤</div>
            <div class="metric-value num-negative">-{{ result.metrics.max_drawdown_pct.toFixed(2) }}%</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">胜率</div>
            <div class="metric-value">{{ result.metrics.win_rate_pct.toFixed(1) }}%</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card class="metric-card" shadow="hover">
            <div class="metric-label">最终权益</div>
            <div class="metric-value">¥{{ fmt(result.metrics.final_equity) }}</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- K 线交易信号 -->
      <el-card v-if="klineData.length" shadow="hover" style="margin-top: 16px">
        <template #header>
          <span style="font-weight: 600">K 线交易信号</span>
          <span style="margin-left: 12px; font-size: 12px; font-weight: 400">
            <span style="color: #d64541">▲ 买入</span>
            <span style="color: #27ae60; margin-left: 8px">▼ 卖出</span>
          </span>
        </template>
        <v-chart :option="klineOption" theme="hengce" style="height: 420px" autoresize />
      </el-card>

      <!-- 权益曲线 + 交易表 -->
      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :xs="24" :md="16">
          <el-card shadow="hover">
            <template #header>
              <span style="font-weight: 600">权益曲线</span>
            </template>
            <v-chart :option="equityOption" theme="hengce" style="height: 380px" autoresize />
          </el-card>
        </el-col>
        <el-col :xs="24" :md="8">
          <el-card shadow="hover" style="height: 100%">
            <template #header>
              <div style="display: flex; justify-content: space-between; align-items: center">
                <span style="font-weight: 600">交易明细</span>
                <el-tag size="small">{{ result.trades.length }} 笔</el-tag>
              </div>
            </template>
            <el-table
              :data="result.trades"
              size="small"
              stripe
              max-height="340"
              :header-cell-style="{ background: '#f5f8fb' }"
            >
              <el-table-column prop="date" label="日期" width="100" />
              <el-table-column label="操作" width="70">
                <template #default="{ row }">
                  <el-tag :type="row.action === 'BUY' ? 'danger' : 'success'" size="small">
                    {{ row.action === 'BUY' ? '买' : '卖' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="价格" width="90" class-name="num">
                <template #default="{ row }">¥{{ row.price.toFixed(2) }}</template>
              </el-table-column>
              <el-table-column label="盈亏" width="100" class-name="num">
                <template #default="{ row }">
                  <span v-if="row.action === 'SELL'" :class="cls(row.pnl)">
                    {{ row.pnl >= 0 ? '+' : '' }}{{ fmt(row.pnl) }}
                  </span>
                  <span v-else style="color: #c0c4cc">—</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>
    </template>

    <el-empty v-else description="配置参数后点击「运行回测」开始策略回测" style="margin-top: 80px" />

    <!-- 回测历史 + 多策略对比 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div class="card-head">
          <span class="card-title">
            <el-icon style="margin-right: 6px"><Files /></el-icon>
            回测历史
          </span>
          <div>
            <el-button
              size="small"
              type="primary"
              :loading="compareLoading"
              :disabled="selectedRows.length < 2 || selectedRows.length > 3"
              @click="openCompare"
            >
              对比选中（{{ selectedRows.length }}）
            </el-button>
            <el-button size="small" @click="loadHistory">
              <el-icon><RefreshRight /></el-icon>
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        v-if="history.length"
        :data="history"
        size="small"
        stripe
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column prop="created_at" label="时间" width="160" />
        <el-table-column prop="symbol" label="股票" width="90" />
        <el-table-column label="策略" min-width="150">
          <template #default="{ row }">
            <div class="stock-cell">
              <span class="stock-name">{{ row.strategy_name }}</span>
              <span class="stock-code">{{ row.strategy }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="收益率" width="100" class-name="num">
          <template #default="{ row }">
            <span :class="cls(row.metrics.total_return_pct)">{{ pct(row.metrics.total_return_pct) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最大回撤" width="100" class-name="num">
          <template #default="{ row }">-{{ row.metrics.max_drawdown_pct.toFixed(2) }}%</template>
        </el-table-column>
        <el-table-column label="夏普" width="80" class-name="num">
          <template #default="{ row }">{{ row.metrics.sharpe_ratio.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="removeHistory(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty
        v-else
        description="暂无历史记录，回测完成后点「保存到历史」"
        :image-size="70"
      />
      <div class="fee-note">
        <el-icon><InfoFilled /></el-icon>
        勾选 2-3 条记录可对比：指标差异 + 权益曲线叠加
      </div>
    </el-card>

    <!-- 对比弹窗 -->
    <el-dialog v-model="compareVisible" title="多策略对比" width="960px" top="6vh">
      <el-table :data="compareRows" size="small" border>
        <el-table-column prop="label" label="指标" width="130" fixed />
        <el-table-column
          v-for="(r, i) in compareRuns"
          :key="r.id"
          :label="`${r.strategy_name}（${r.symbol}）`"
          min-width="160"
        >
          <template #default="{ row }">{{ row.values[i] }}</template>
        </el-table-column>
      </el-table>
      <v-chart :option="compareOption" theme="hengce" style="height: 360px; margin-top: 12px" autoresize />
      <template #footer>
        <el-button @click="compareVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Promotion, InfoFilled, Briefcase } from '@element-plus/icons-vue'
import {
  backtestApi,
  type BacktestHistoryDetail,
  type BacktestHistoryItem,
  type BacktestResult,
  type StrategyInfo,
} from '@/api/backtest'
import { portfolioApi } from '@/api/portfolio'
import { marketApi } from '@/api/market'
import type { KlinePoint } from '@/types'

const router = useRouter()

const running = ref(false)
const result = ref<BacktestResult | null>(null)
// K 线数据（回测成功后拉取，用于叠加买卖信号）
const klineData = ref<KlinePoint[]>([])
const strategyList = ref<StrategyInfo[]>([])
const groups = ref<string[]>([])

// 回测历史 + 对比
const history = ref<BacktestHistoryItem[]>([])
const selectedRows = ref<BacktestHistoryItem[]>([])
const saving = ref(false)
const compareVisible = ref(false)
const compareLoading = ref(false)
const compareRuns = ref<BacktestHistoryDetail[]>([])

const form = reactive({
  symbol: '600519',
  strategy: 'ma_cross',
  initial_capital: 200_000,
  days: 120,
  params: {} as Record<string, number>,
})

// 从衡策智算的「用回测验证」跳转过来时，自动带入股票代码
const route = useRoute()
const querySymbol = route.query.symbol
if (typeof querySymbol === 'string' && /^\d{6}$/.test(querySymbol)) {
  form.symbol = querySymbol
}

const currentStrategy = computed(() =>
  strategyList.value.find(s => s.id === form.strategy) || null,
)
const currentParams = computed(() => currentStrategy.value?.params || [])

function strategiesOf(g: string) {
  return strategyList.value.filter(s => s.group === g)
}

function onStrategyChange() {
  form.params = {}
  for (const p of currentParams.value) {
    form.params[p.name] = p.default
  }
}

async function loadStrategies() {
  try {
    const res = await backtestApi.strategies()
    strategyList.value = res.strategies
    groups.value = res.groups
    onStrategyChange()
  } catch { /* ignore */ }
}

async function runBacktest() {
  if (!form.symbol || !/^\d{6}$/.test(form.symbol)) {
    ElMessage.warning('请输入 6 位股票代码')
    return
  }
  running.value = true
  try {
    result.value = await backtestApi.run({
      symbol: form.symbol,
      strategy: form.strategy,
      initial_capital: form.initial_capital,
      days: form.days,
      params: form.params,
    })
    ElMessage.success(`回测完成！共 ${result.value.trades.length} 笔交易，收益率 ${result.value.metrics.total_return_pct.toFixed(2)}%`)
    loadKline()
  } catch {
    result.value = null
    klineData.value = []
  } finally {
    running.value = false
  }
}

/** 拉取回测窗口内的日 K，供交易信号叠加（失败不影响回测结果展示） */
async function loadKline() {
  klineData.value = []
  if (!result.value) return
  try {
    const res = await marketApi.history(form.symbol, form.days * 2 + 60)
    const bars = res?.data || []
    const curve = result.value.equity_curve
    if (curve.length) {
      const from = curve[0].date
      const to = curve[curve.length - 1].date
      klineData.value = bars.filter(b => b.date >= from && b.date <= to)
    } else {
      klineData.value = bars
    }
  } catch {
    /* K 线加载失败时隐藏信号图，回测结果照常展示 */
  }
}

const openingPortfolio = ref(false)

const hasBuySignal = computed(() =>
  !!result.value?.trades?.some(t => t.action === 'BUY'),
)

async function openFromBacktest() {
  if (!result.value || !hasBuySignal.value) return
  const firstBuy = result.value.trades.find(t => t.action === 'BUY')!
  try {
    await ElMessageBox.confirm(
      `将按回测首笔买入信号开模拟仓：
${result.value.symbol} · ${firstBuy.shares} 股 · ¥${firstBuy.price.toFixed(2)}/股
总金额 ¥${(firstBuy.shares * firstBuy.price).toFixed(2)}`,
      '一键开模拟仓',
      { type: 'info', confirmButtonText: '确认开仓', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  openingPortfolio.value = true
  try {
    const res = await portfolioApi.fromBacktest({
      symbol: result.value.symbol,
      strategy: result.value.strategy,
      trades: result.value.trades,
      initial_capital: form.initial_capital,
    })
    ElMessage.success(`模拟仓已创建：${res.shares} 股 @ ¥${res.price.toFixed(2)}`)
    router.push('/portfolio')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '开仓失败')
  } finally {
    openingPortfolio.value = false
  }
}

onMounted(() => {
  loadStrategies()
  loadHistory()
})

async function loadHistory() {
  try {
    const res = await backtestApi.history()
    history.value = res.items
  } catch {
    /* 错误提示已由拦截器统一处理 */
  }
}

async function saveCurrent() {
  if (!result.value) return
  saving.value = true
  try {
    await backtestApi.save(result.value, form.days)
    ElMessage.success('已保存到回测历史')
    await loadHistory()
  } finally {
    saving.value = false
  }
}

function onSelectionChange(rows: BacktestHistoryItem[]) {
  selectedRows.value = rows
}

async function removeHistory(row: BacktestHistoryItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除该回测记录（${row.strategy_name} · ${row.symbol}）？`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await backtestApi.remove(row.id)
  ElMessage.success('已删除')
  await loadHistory()
}

async function openCompare() {
  const ids = selectedRows.value.map(r => r.id)
  if (ids.length < 2 || ids.length > 3) return
  compareLoading.value = true
  try {
    const res = await backtestApi.compare(ids)
    compareRuns.value = res.items
    compareVisible.value = true
  } finally {
    compareLoading.value = false
  }
}

/** 对比表：每行一个指标，每列一条回测记录 */
const compareRows = computed(() => {
  const runs = compareRuns.value
  if (!runs.length) return []
  return [
    { label: '股票 / 策略', values: runs.map(r => `${r.symbol} · ${r.strategy_name}`) },
    { label: '总收益率', values: runs.map(r => pct(r.metrics.total_return_pct)) },
    { label: '年化收益率', values: runs.map(r => pct(r.metrics.annual_return_pct)) },
    { label: '夏普比率', values: runs.map(r => r.metrics.sharpe_ratio.toFixed(2)) },
    { label: '最大回撤', values: runs.map(r => `-${r.metrics.max_drawdown_pct.toFixed(2)}%`) },
    { label: '胜率', values: runs.map(r => `${r.metrics.win_rate_pct.toFixed(1)}%`) },
    { label: '最终权益', values: runs.map(r => `¥${fmt(r.metrics.final_equity)}`) },
    { label: '回测天数', values: runs.map(r => `${r.days} 天`) },
  ]
})

/** 对比图：多条权益曲线按日期对齐叠加 */
const compareOption = computed(() => {
  const runs = compareRuns.value
  if (!runs.length) return {}
  const colors = ['#d64541', '#1f4e79', '#2e8b74']

  const dates = new Set<string>()
  runs.forEach(r => r.equity_curve.forEach(p => dates.add(p.date)))
  const xData = [...dates].sort()

  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 60, right: 20, top: 45, bottom: 40 },
    xAxis: {
      type: 'category',
      data: xData.map(d => d.slice(5)),
      axisLabel: { rotate: 30, fontSize: 11 },
      axisLine: { lineStyle: { color: '#dcdfe6' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { formatter: (v: number) => `¥${(v / 1000).toFixed(0)}k` },
      splitLine: { lineStyle: { color: '#f0f2f5' } },
    },
    series: runs.map((r, i) => {
      const equityMap = new Map(r.equity_curve.map(p => [p.date, p.equity]))
      return {
        name: `${r.strategy_name}（${r.symbol}）`,
        type: 'line',
        smooth: true,
        symbol: 'none',
        connectNulls: true,
        data: xData.map(d => equityMap.get(d) ?? null),
        lineStyle: { width: 2, color: colors[i] },
        itemStyle: { color: colors[i] },
      }
    }),
  }
})

// K 线 + 买卖信号图
const klineOption = computed(() => {
  const bars = klineData.value
  if (!bars.length || !result.value) return {}

  const trades = result.value.trades
  const dates = bars.map(b => b.date.slice(5))
  // ECharts candlestick 数据约定：[open, close, low, high]
  const ohlc = bars.map(b => [b.open, b.close, b.low, b.high])

  const barByDate = new Map(bars.map(b => [b.date, b]))
  const tradeMap = new Map<string, typeof trades>()
  for (const t of trades) {
    const arr = tradeMap.get(t.date) || []
    arr.push(t)
    tradeMap.set(t.date, arr)
  }

  // 买点标在当日最低价下方（红▲），卖点标在最高价上方（绿▼）
  const buys: any[] = []
  const sells: any[] = []
  for (const t of trades) {
    const bar = barByDate.get(t.date)
    if (!bar) continue
    if (t.action === 'BUY') {
      buys.push({ coord: [t.date.slice(5), bar.low * 0.98], value: t.price })
    } else {
      sells.push({ coord: [t.date.slice(5), bar.high * 1.02], value: t.price })
    }
  }

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        const k = params.find(p => p.seriesType === 'candlestick')
        if (!k) return ''
        const [o, c, l, h] = (k.data as number[]).slice(-4)
        const fullDate = bars[k.dataIndex].date
        const lines = [
          fullDate,
          `开 ${o.toFixed(2)}　高 ${h.toFixed(2)}`,
          `低 ${l.toFixed(2)}　收 ${c.toFixed(2)}`,
        ]
        for (const t of tradeMap.get(fullDate) || []) {
          lines.push(
            `${t.action === 'BUY' ? '▲ 买入' : '▼ 卖出'} ¥${t.price.toFixed(2)}` +
              (t.shares ? ` × ${t.shares} 股` : ''),
          )
        }
        return lines.join('<br/>')
      },
    },
    grid: { left: 60, right: 30, top: 30, bottom: 40 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { rotate: 30, fontSize: 11 },
      axisLine: { lineStyle: { color: '#dcdfe6' } },
    },
    yAxis: {
      scale: true,
      splitLine: { lineStyle: { color: '#f0f2f5' } },
      splitArea: { show: false },
    },
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: ohlc,
        // A 股配色：阳线红、阴线绿
        itemStyle: {
          color: '#e64545',
          color0: '#27ae60',
          borderColor: '#e64545',
          borderColor0: '#27ae60',
        },
      },
      {
        name: '买入',
        type: 'scatter',
        symbol: 'triangle',
        symbolSize: 13,
        itemStyle: { color: '#d64541', borderColor: '#fff', borderWidth: 1 },
        data: buys,
        z: 10,
      },
      {
        name: '卖出',
        type: 'scatter',
        symbol: 'triangle',
        symbolRotate: 180,
        symbolSize: 13,
        itemStyle: { color: '#27ae60', borderColor: '#fff', borderWidth: 1 },
        data: sells,
        z: 10,
      },
    ],
  }
})

// 权益曲线图表
const equityOption = computed(() => {
  if (!result.value) return {}
  const curve = result.value.equity_curve
  const dates = curve.map(p => p.date.slice(5))
  const equities = curve.map(p => p.equity)

  const init = result.value.metrics.initial_capital
  const final = result.value.metrics.final_equity
  const lineColor = final >= init ? '#e74c3c' : '#27ae60'

  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any[]) => {
        const p = params[0]
        return `${p.axisValue}<br/>权益: ¥${Number(p.value).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`
      },
    },
    grid: { left: 60, right: 30, top: 20, bottom: 40 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { rotate: 30, fontSize: 11 },
      axisLine: { lineStyle: { color: '#dcdfe6' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: (v: number) => `¥${(v / 1000).toFixed(0)}k`,
      },
      splitLine: { lineStyle: { color: '#f0f2f5' } },
    },
    series: [{
      type: 'line',
      data: equities,
      smooth: true,
      symbol: 'none',
      lineStyle: { color: lineColor, width: 2 },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: lineColor + '40' },
            { offset: 1, color: lineColor + '05' },
          ],
        },
      },
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: '#909399', type: 'dashed', width: 1 },
        data: [{ yAxis: init, label: { formatter: '初始资金', position: 'end' } }],
      },
    }],
  }
})

// 工具函数
function fmt(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '0.00'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function pct(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}
function cls(v: number): string {
  return v > 0 ? 'num-positive' : v < 0 ? 'num-negative' : ''
}
</script>

<style scoped>
.fee-note {
  margin-top: 12px;
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;
  gap: 4px;
}
.cards {
  margin-bottom: 0;
}
.metric-card {
  text-align: center;
  border-radius: 8px;
}
.metric-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 8px;
}
.metric-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
}
.result-bar {
  margin-top: 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.result-summary {
  display: flex;
  align-items: center;
  gap: 10px;
}
.muted-text {
  font-size: 13px;
  color: #909399;
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.card-title {
  font-weight: 600;
  font-size: 16px;
  display: flex;
  align-items: center;
}
.stock-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.stock-name {
  font-weight: 600;
}
.stock-code {
  font-size: 12px;
  color: #909399;
}
</style>
