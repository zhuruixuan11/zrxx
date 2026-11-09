<template>
  <div class="benchmark-page">
    <!-- 分析设置 -->
    <el-card shadow="hover">
      <template #header>
        <div class="card-head">
          <span class="card-title">
            <el-icon style="margin-right: 6px"><Aim /></el-icon>
            基准跟随 · 跟踪误差分析
          </span>
          <div class="head-tags">
            <el-tag type="info" size="small">真实行情</el-tag>
            <el-tag type="warning" size="small">Alpha 无风险利率 = 0</el-tag>
          </div>
        </div>
      </template>

      <el-form :model="form" label-width="90px" inline>
        <el-form-item label="分析对象">
          <el-select v-model="form.source" style="width: 150px">
            <el-option label="策略回测（现跑）" value="strategy" />
            <el-option label="已保存回测" value="run" />
            <el-option label="我的组合" value="portfolio" />
          </el-select>
        </el-form-item>

        <template v-if="form.source === 'strategy'">
          <el-form-item label="股票代码">
            <el-input v-model="form.symbol" placeholder="如 600519" style="width: 130px" maxlength="6" />
          </el-form-item>
          <el-form-item label="策略">
            <el-select v-model="form.strategy" style="width: 190px">
              <el-option
                v-for="s in strategyList.filter(x => !x.disabled)"
                :key="s.id"
                :label="s.name"
                :value="s.id"
              />
            </el-select>
          </el-form-item>
        </template>

        <el-form-item v-if="form.source === 'run'" label="回测记录">
          <el-select v-model="form.run_id" placeholder="选择一条已保存回测" style="width: 260px">
            <el-option
              v-for="h in history"
              :key="h.id"
              :label="`${h.strategy_name} · ${h.symbol} · ${h.created_at || ''}`"
              :value="h.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="对比基准">
          <el-select v-model="form.benchmark" style="width: 180px">
            <el-option
              v-for="b in options.benchmarks"
              :key="b.id"
              :label="b.name"
              :value="b.id"
              :disabled="!b.available"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="区间天数">
          <el-input-number v-model="form.days" :min="20" :max="500" :step="20" style="width: 120px" />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="loading" @click="analyze">
            <el-icon><DataAnalysis /></el-icon>
            开始分析
          </el-button>
        </el-form-item>
      </el-form>

      <div class="hint">
        <el-icon><InfoFilled /></el-icon>
        {{ options.data_source_note }}
      </div>
    </el-card>

    <template v-if="result">
      <!-- 指标 -->
      <el-card shadow="hover" style="margin-top: 16px">
        <template #header>
          <div class="card-head">
            <span class="card-title">核心指标</span>
            <span class="muted">
              {{ result.period.start }} ~ {{ result.period.end }} ·
              {{ result.period.trading_days }} 个交易日
            </span>
          </div>
        </template>

        <div class="metric-grid">
          <div class="metric">
            <div class="label">策略累计收益</div>
            <div class="value" :class="pnlClass(result.metrics.strategy.cumulative_return_pct)">
              {{ fmtPct(result.metrics.strategy.cumulative_return_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">基准累计收益</div>
            <div class="value" :class="pnlClass(result.metrics.benchmark.cumulative_return_pct)">
              {{ fmtPct(result.metrics.benchmark.cumulative_return_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">累计超额</div>
            <div class="value" :class="pnlClass(result.metrics.excess.cumulative_pct)">
              {{ fmtPct(result.metrics.excess.cumulative_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">策略年化（几何）</div>
            <div class="value" :class="pnlClass(result.metrics.strategy.annualized_return_pct)">
              {{ fmtPct(result.metrics.strategy.annualized_return_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">基准年化（几何）</div>
            <div class="value" :class="pnlClass(result.metrics.benchmark.annualized_return_pct)">
              {{ fmtPct(result.metrics.benchmark.annualized_return_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">年化超额</div>
            <div class="value" :class="pnlClass(result.metrics.excess.annualized_pct)">
              {{ fmtPct(result.metrics.excess.annualized_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">跟踪误差（年化）</div>
            <div class="value">{{ result.metrics.excess.tracking_error_pct.toFixed(2) }}%</div>
          </div>
          <div class="metric">
            <div class="label">Beta</div>
            <div class="value">{{ result.metrics.excess.beta.toFixed(3) }}</div>
          </div>
          <div class="metric">
            <div class="label">Alpha（rf=0）</div>
            <div class="value" :class="pnlClass(result.metrics.excess.alpha_pct)">
              {{ fmtPct(result.metrics.excess.alpha_pct) }}
            </div>
          </div>
          <div class="metric">
            <div class="label">策略最大回撤</div>
            <div class="value down">{{ result.metrics.strategy.max_drawdown_pct.toFixed(2) }}%</div>
          </div>
          <div class="metric">
            <div class="label">基准最大回撤</div>
            <div class="value down">{{ result.metrics.benchmark.max_drawdown_pct.toFixed(2) }}%</div>
          </div>
          <div class="metric">
            <div class="label">超额收益曲线末值</div>
            <div class="value" :class="pnlClass(result.series.at(-1)?.excess_pct ?? 0)">
              {{ fmtPct(result.series.at(-1)?.excess_pct ?? 0) }}
            </div>
          </div>
        </div>
      </el-card>

      <!-- 图 -->
      <el-card shadow="hover" style="margin-top: 16px">
        <template #header>
          <div class="card-head">
            <span class="card-title">净值曲线对比</span>
            <span class="muted">起点归一 = 1.0</span>
          </div>
        </template>
        <v-chart :option="navOption" theme="hengce" style="height: 380px" autoresize />
      </el-card>

      <el-card shadow="hover" style="margin-top: 16px">
        <template #header>
          <div class="card-head">
            <span class="card-title">超额收益曲线</span>
            <span class="muted">策略净值 / 基准净值 - 1</span>
          </div>
        </template>
        <v-chart :option="excessOption" theme="hengce" style="height: 300px" autoresize />
      </el-card>

      <!-- 行业 -->
      <el-card shadow="hover" style="margin-top: 16px">
        <template #header>
          <div class="card-head">
            <span class="card-title">行业暴露</span>
            <el-tag size="small" :type="result.industry.benchmark_available ? 'success' : 'info'">
              {{ result.industry.benchmark_available ? '含行业偏离' : '仅组合暴露' }}
            </el-tag>
          </div>
        </template>

        <el-alert
          :title="result.industry.note"
          type="info"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        />

        <el-table v-if="result.industry.items.length" :data="result.industry.items" size="small" stripe>
          <el-table-column prop="industry" label="行业" min-width="140" />
          <el-table-column label="组合权重" width="120" class-name="num">
            <template #default="{ row }">{{ row.weight_pct.toFixed(2) }}%</template>
          </el-table-column>
          <el-table-column label="基准权重" width="120" class-name="num">
            <template #default="{ row }">
              <span v-if="row.benchmark_weight_pct != null">{{ row.benchmark_weight_pct.toFixed(2) }}%</span>
              <span v-else class="muted">不可用</span>
            </template>
          </el-table-column>
          <el-table-column label="偏离" width="120" class-name="num">
            <template #default="{ row }">
              <span v-if="row.deviation_pct != null" :class="pnlClass(row.deviation_pct)">
                {{ fmtPct(row.deviation_pct) }}
              </span>
              <span v-else class="muted">不计算</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="当前分析对象没有可用的行业权重" :image-size="70" />
      </el-card>

      <!-- 口径与来源 -->
      <el-card shadow="hover" style="margin-top: 16px">
        <template #header>
          <span class="card-title">口径与数据来源</span>
        </template>
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="数据来源">
            策略：{{ result.data_sources.strategy }}<br />
            基准：{{ result.benchmark.name }}（{{ result.data_sources.benchmark }}）
            <template v-if="result.data_sources.industry">
              <br />行业：{{ result.data_sources.industry }}
            </template>
          </el-descriptions-item>
          <el-descriptions-item
            v-for="(text, key) in result.formulas"
            :key="key"
            :label="formulaLabel(String(key))"
          >
            {{ text }}
          </el-descriptions-item>
        </el-descriptions>
        <ul class="notes">
          <li v-for="(n, i) in result.notes" :key="i">{{ n }}</li>
        </ul>
      </el-card>
    </template>

    <el-empty v-else description="选择分析对象与基准后点击「开始分析」" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Aim, DataAnalysis, InfoFilled } from '@element-plus/icons-vue'
import {
  benchmarkApi,
  type BenchmarkAnalysis,
  type BenchmarkId,
  type BenchmarkOptions,
  type BenchmarkSource,
} from '@/api/benchmark'
import { backtestApi, type BacktestHistoryItem, type StrategyInfo } from '@/api/backtest'

const loading = ref(false)
const result = ref<BenchmarkAnalysis | null>(null)
const strategyList = ref<StrategyInfo[]>([])
const history = ref<BacktestHistoryItem[]>([])

const options = ref<BenchmarkOptions>({
  benchmarks: [],
  holdings_count: 0,
  formulas: {},
  data_source_note: '',
})

const form = reactive({
  source: 'strategy' as BenchmarkSource,
  benchmark: 'hs300' as BenchmarkId,
  symbol: '600519',
  strategy: 'ma_cross',
  run_id: '',
  days: 120,
})

onMounted(async () => {
  try {
    options.value = await benchmarkApi.options()
  } catch { /* 拦截器已提示 */ }
  try {
    const res = await backtestApi.strategies()
    strategyList.value = res.strategies
  } catch { /* ignore */ }
  try {
    const res = await backtestApi.history(30)
    history.value = res.items
  } catch { /* ignore */ }
})

async function analyze() {
  if (form.source === 'strategy' && !/^\d{6}$/.test(form.symbol)) {
    ElMessage.warning('请输入 6 位股票代码')
    return
  }
  if (form.source === 'run' && !form.run_id) {
    ElMessage.warning('请选择一条已保存的回测记录')
    return
  }
  loading.value = true
  try {
    result.value = await benchmarkApi.analyze({
      source: form.source,
      benchmark: form.benchmark,
      symbol: form.symbol,
      strategy: form.strategy,
      days: form.days,
      run_id: form.run_id || undefined,
    })
    ElMessage.success('分析完成')
  } catch {
    result.value = null
  } finally {
    loading.value = false
  }
}

const navOption = computed(() => {
  const s = result.value?.series || []
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['策略净值', '基准净值'], top: 0 },
    grid: { left: 60, right: 30, top: 40, bottom: 50 },
    xAxis: {
      type: 'category',
      data: s.map(p => p.date),
      axisLabel: { rotate: 30, fontSize: 11 },
      axisLine: { lineStyle: { color: '#dcdfe6' } },
    },
    yAxis: {
      scale: true,
      splitLine: { lineStyle: { color: '#f0f2f5' } },
    },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 4 }],
    series: [
      {
        name: '策略净值',
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: s.map(p => p.strategy_nav),
        lineStyle: { width: 2, color: '#1f4e79' },
        itemStyle: { color: '#1f4e79' },
      },
      {
        name: '基准净值',
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: s.map(p => p.benchmark_nav),
        lineStyle: { width: 2, color: '#c8a45c' },
        itemStyle: { color: '#c8a45c' },
      },
    ],
  }
})

const excessOption = computed(() => {
  const s = result.value?.series || []
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: number) => `${v.toFixed(2)}%`,
    },
    grid: { left: 60, right: 30, top: 30, bottom: 50 },
    xAxis: {
      type: 'category',
      data: s.map(p => p.date),
      axisLabel: { rotate: 30, fontSize: 11 },
      axisLine: { lineStyle: { color: '#dcdfe6' } },
    },
    yAxis: {
      splitLine: { lineStyle: { color: '#f0f2f5' } },
      axisLabel: { formatter: '{value}%' },
    },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 4 }],
    series: [
      {
        name: '超额收益',
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: s.map(p => p.excess_pct),
        lineStyle: { width: 2, color: '#d64541' },
        itemStyle: { color: '#d64541' },
        areaStyle: { opacity: 0.12 },
        markLine: {
          silent: true,
          symbol: 'none',
          label: { formatter: '0%' },
          lineStyle: { type: 'dashed', color: '#9aa7b4' },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  }
})

const FORMULA_LABELS: Record<string, string> = {
  daily_return: '日收益',
  cumulative_return: '累计收益',
  annualized_return: '年化收益',
  excess_return: '累计超额',
  annualized_excess: '年化超额',
  tracking_error: '跟踪误差',
  beta: 'Beta',
  alpha: 'Alpha',
  max_drawdown: '最大回撤',
}

function formulaLabel(key: string) {
  return FORMULA_LABELS[key] || key
}

function fmtPct(v: number) {
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pnlClass(v: number) {
  if (v > 0) return 'up'
  if (v < 0) return 'down'
  return ''
}
</script>

<style scoped>
.benchmark-page {
  max-width: 1280px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  font-weight: 600;
  color: #1f4e79;
}
.head-tags {
  display: flex;
  gap: 8px;
  align-items: center;
}
.hint {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #7a8896;
  font-size: 12px;
  margin-top: 4px;
}
.muted {
  color: #8a97a5;
  font-size: 12px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}
.metric {
  background: #f7f9fc;
  border: 1px solid #e8eef5;
  border-radius: 6px;
  padding: 12px 14px;
}
.metric .label {
  font-size: 12px;
  color: #7a8896;
  margin-bottom: 6px;
}
.metric .value {
  font-size: 20px;
  font-weight: 600;
  color: #20303f;
  font-variant-numeric: tabular-nums;
}
.num {
  font-variant-numeric: tabular-nums;
}
.up {
  color: #d64541;
}
.down {
  color: #2e8b74;
}
.notes {
  margin: 10px 0 0;
  padding-left: 18px;
  color: #7a8896;
  font-size: 12px;
  line-height: 1.8;
}
</style>
