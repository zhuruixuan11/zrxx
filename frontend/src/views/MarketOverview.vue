<template>
  <div class="market-overview">
    <el-card shadow="hover">
      <template #header>
        <div class="header-row">
          <div class="header-title">
            <el-icon style="margin-right: 6px"><Grid /></el-icon>
            A 股全景 · 实时行情
          </div>
          <div class="header-note">数据源：新浪 + 同花顺 + 腾讯 + 东方财富（全部免费）</div>
        </div>
      </template>

      <el-tabs v-model="tab" @tab-change="handleTabChange">
        <!-- ============ Tab 1: 全景排行榜 ============ -->
        <el-tab-pane label="全部股票" name="screener">
          <div class="filter-bar">
            <el-input
              v-model="screenerParams.search"
              placeholder="搜索代码或名称，如 600519 / 茅台"
              clearable
              style="width: 220px"
              @keyup.enter="loadScreener"
            />
            <el-select v-model="screenerParams.market" placeholder="全部市场" clearable style="width: 110px">
              <el-option label="沪市 SH" value="SH" />
              <el-option label="深市 SZ" value="SZ" />
              <el-option label="北交 BJ" value="BJ" />
            </el-select>
            <el-select v-model="sortBy" style="width: 130px">
              <el-option label="涨跌幅" value="change_pct" />
              <el-option label="成交额" value="amount" />
              <el-option label="成交量" value="volume" />
              <el-option label="最新价" value="price" />
              <el-option label="代码" value="code" />
            </el-select>
            <el-select v-model="sortOrder" style="width: 100px">
              <el-option label="降序" value="desc" />
              <el-option label="升序" value="asc" />
            </el-select>
            <el-button type="primary" :icon="Search" :loading="loading" @click="loadScreener">查询</el-button>
            <el-tag v-if="screenerResult" type="info" size="large" style="margin-left: auto">
              共 {{ screenerResult.total.toLocaleString() }} 只
            </el-tag>
          </div>

          <el-table
            :data="screenerResult?.items || []"
            v-loading="loading"
            stripe
            size="small"
            :default-sort="{ prop: 'change_pct', order: 'descending' }"
            height="calc(100vh - 360px)"
          >
            <el-table-column prop="code" label="代码" width="90" />
            <el-table-column prop="name" label="名称" width="120" />
            <el-table-column label="市场" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="marketTag(row.market)">{{ row.market }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最新价" width="100" align="right">
              <template #default="{ row }">¥{{ row.price.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="涨跌额" width="100" align="right">
              <template #default="{ row }">
                <span :class="cls(row.change)">{{ row.change >= 0 ? '+' : '' }}{{ row.change.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="涨跌幅" width="100" align="right">
              <template #default="{ row }">
                <span class="change-badge" :class="cls(row.change_pct)">
                  {{ row.change_pct >= 0 ? '+' : '' }}{{ row.change_pct.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="今开" width="90" align="right">
              <template #default="{ row }">{{ row.open.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="最高" width="90" align="right">
              <template #default="{ row }"><span class="num-positive">{{ row.high.toFixed(2) }}</span></template>
            </el-table-column>
            <el-table-column label="最低" width="90" align="right">
              <template #default="{ row }"><span class="num-negative">{{ row.low.toFixed(2) }}</span></template>
            </el-table-column>
            <el-table-column label="成交量(万)" width="110" align="right">
              <template #default="{ row }">{{ (row.volume / 10000).toFixed(1) }}</template>
            </el-table-column>
            <el-table-column label="成交额(亿)" width="110" align="right">
              <template #default="{ row }">{{ (row.amount / 1e8).toFixed(2) }}</template>
            </el-table-column>
          </el-table>

          <div class="pager-bar">
            <el-pagination
              v-model:current-page="screenerParams.page"
              v-model:page-size="screenerParams.size"
              :page-sizes="[20, 30, 50, 100]"
              :total="screenerResult?.total || 0"
              layout="total, sizes, prev, pager, next"
              background
              @size-change="loadScreener"
              @current-change="loadScreener"
            />
          </div>
        </el-tab-pane>

        <!-- ============ Tab 2: 概念板块 ============ -->
        <el-tab-pane label="概念板块" name="concept">
          <el-table
            :data="conceptItems"
            v-loading="conceptLoading"
            stripe
            size="small"
            height="calc(100vh - 300px)"
          >
            <el-table-column prop="name" label="概念" min-width="160" />
            <el-table-column label="涨跌" width="110" align="right" sortable :default-sort="{ order: 'descending' }">
              <template #default="{ row }">
                <span class="change-badge" :class="cls(row.change_pct)">
                  {{ row.change_pct >= 0 ? '+' : '' }}{{ row.change_pct.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="company_count" label="家数" width="70" align="right" />
            <el-table-column label="成交额(亿)" width="120" align="right">
              <template #default="{ row }">{{ (row.amount / 1e8).toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="龙头股" width="200">
              <template #default="{ row }">
                <span class="lead-name">{{ row.lead_stock_name }}</span>
                <span class="lead-code" :class="cls(row.lead_stock_change_pct)">
                  {{ row.lead_stock_code }}
                  {{ row.lead_stock_change_pct >= 0 ? '+' : '' }}{{ row.lead_stock_change_pct.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ============ Tab 3: 涨停池 ============ -->
        <el-tab-pane label="涨停池" name="zt">
          <el-table
            :data="ztItems"
            v-loading="ztLoading"
            stripe
            size="small"
            height="calc(100vh - 300px)"
          >
            <el-table-column prop="code" label="代码" width="90" />
            <el-table-column prop="name" label="名称" width="120" />
            <el-table-column label="涨跌幅" width="100" align="right">
              <template #default="{ row }">
                <span class="change-badge num-positive">{{ row.change_pct.toFixed(2) }}%</span>
              </template>
            </el-table-column>
            <el-table-column label="最新价" width="90" align="right">
              <template #default="{ row }">¥{{ row.price.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="连板" width="70" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.consecutive > 1" type="danger" size="small">{{ row.consecutive }}板</el-tag>
                <span v-else>1板</span>
              </template>
            </el-table-column>
            <el-table-column label="封板资金(亿)" width="120" align="right">
              <template #default="{ row }">{{ (row.fund_sealed / 1e8).toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="炸板" width="70" align="center">
              <template #default="{ row }">
                <span :class="row.broken_times > 0 ? 'num-warning' : ''">{{ row.broken_times }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="first_seal_time" label="首次封板" width="100" />
            <el-table-column label="换手率" width="90" align="right">
              <template #default="{ row }">{{ row.turnover.toFixed(2) }}%</template>
            </el-table-column>
            <el-table-column prop="industry" label="所属行业" min-width="120" />
          </el-table>
        </el-tab-pane>

        <!-- ============ Tab 4: 策略选股 ============ -->
        <el-tab-pane label="策略选股" name="strategic">
          <div class="filter-bar">
            <el-select v-model="ssSignal" placeholder="选择策略信号" style="width: 240px">
              <el-option
                v-for="s in ssSignals"
                :key="s.id"
                :label="`${s.name} · ${s.desc}`"
                :value="s.id"
              />
            </el-select>
            <el-select v-model="ssTopN" style="width: 100px">
              <el-option label="Top 10" :value="10" />
              <el-option label="Top 20" :value="20" />
              <el-option label="Top 30" :value="30" />
            </el-select>
            <el-button type="primary" :loading="ssLoading" @click="runStrategicScan">
              <el-icon><Search /></el-icon>
              扫描
            </el-button>
            <el-tag v-if="ssResult" type="success" size="large" style="margin-left: auto">
              扫描 {{ ssResult.total_scanned.toLocaleString() }} 只，命中 {{ ssResult.results.length }} 只
            </el-tag>
          </div>
          <div v-if="ssResult" class="ss-desc">
            <el-icon style="vertical-align: -2px"><InfoFilled /></el-icon>
            信号「{{ ssResult.signal_name }}」：{{ ssResult.signal_desc }}
          </div>

          <el-table
            v-if="ssResult"
            :data="ssResult.results"
            v-loading="ssLoading"
            stripe
            size="small"
            height="calc(100vh - 360px)"
          >
            <el-table-column prop="code" label="代码" width="90" />
            <el-table-column prop="name" label="名称" width="130" />
            <el-table-column label="最新价" width="100" align="right">
              <template #default="{ row }">¥{{ (row.price ?? 0).toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="涨跌幅" width="100" align="right">
              <template #default="{ row }">
                <span class="change-badge" :class="cls(row.change_pct)">
                  {{ row.change_pct >= 0 ? '+' : '' }}{{ row.change_pct.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="成交额(亿)" width="110" align="right">
              <template #default="{ row }">{{ ((row.amount ?? 0) / 1e8).toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="180">
              <template #default="{ row }">
                <el-button size="small" type="primary" link @click="goMarket(row.code)">行情</el-button>
                <el-button size="small" type="success" link @click="goBacktest(row.code)">回测</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else-if="!ssLoading" description="选择信号并点击扫描" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Search, Grid, InfoFilled } from '@element-plus/icons-vue'
import { marketApi, type ScreenerResult, type ConceptItem, type ZtItem } from '@/api/market'

const router = useRouter()

const tab = ref('screener')
const loading = ref(false)
const screenerResult = ref<ScreenerResult | null>(null)
const sortBy = ref('change_pct')
const sortOrder = ref('desc')
const screenerParams = reactive({ page: 1, size: 30, search: '', market: '' })

const conceptItems = ref<ConceptItem[]>([])
const ztItems = ref<ZtItem[]>([])
const conceptLoading = ref(false)
const ztLoading = ref(false)

// ===== 策略选股 =====
const ssSignals = ref<Array<{ id: string; name: string; desc: string; need_days: number }>>([])
const ssSignal = ref('breakout_20d')
const ssTopN = ref(15)
const ssLoading = ref(false)
const ssResult = ref<{
  signal: string
  signal_name: string
  signal_desc: string
  total_scanned: number
  results: any[]
} | null>(null)

async function loadScreener() {
  loading.value = true
  try {
    screenerResult.value = await marketApi.screener({
      ...screenerParams,
      sort_by: sortBy.value,
      sort_order: sortOrder.value,
    })
  } catch { /* interceptor handles */ }
  finally { loading.value = false }
}

async function loadConcept() {
  conceptLoading.value = true
  try {
    const res = await marketApi.boardsConcept()
    conceptItems.value = res.items
  } catch { /* ignore */ }
  finally { conceptLoading.value = false }
}

async function loadZt() {
  ztLoading.value = true
  try {
    const res = await marketApi.boardsZt()
    ztItems.value = res.items
  } catch { /* ignore */ }
  finally { ztLoading.value = false }
}

async function loadSignals() {
  try {
    const res = await marketApi.screenerSignals()
    ssSignals.value = res.signals
  } catch { /* ignore */ }
}

async function runStrategicScan() {
  if (!ssSignal.value) return
  ssLoading.value = true
  ssResult.value = null
  try {
    ssResult.value = await marketApi.screenerStrategic({
      signal: ssSignal.value,
      top_n: ssTopN.value,
    })
  } catch { /* ignore */ }
  finally { ssLoading.value = false }
}

function goMarket(code: string) {
  router.push({ path: '/market', query: { symbol: code } })
}

function goBacktest(code: string) {
  router.push({ path: '/backtest', query: { symbol: code } })
}

function handleTabChange(t: string) {
  if (t === 'concept' && conceptItems.value.length === 0) loadConcept()
  if (t === 'zt' && ztItems.value.length === 0) loadZt()
  if (t === 'strategic' && ssSignals.value.length === 0) loadSignals()
}

function marketTag(m: string) {
  const map: Record<string, '' | 'success' | 'warning' | 'info'> = { SH: '', SZ: 'success', BJ: 'warning' }
  return map[m] || 'info'
}
function cls(v: number) {
  return v > 0 ? 'num-positive' : v < 0 ? 'num-negative' : ''
}

onMounted(loadScreener)
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center }
.header-title { font-weight: 600; font-size: 16px }
.header-note { font-size: 12px; color: #909399 }

.filter-bar {
  display: flex; gap: 10px; align-items: center;
  padding: 12px 0; border-bottom: 1px solid #ebeef5; margin-bottom: 12px;
}

.pager-bar { display: flex; justify-content: flex-end; padding: 12px 0 }

.change-badge {
  display: inline-block; min-width: 62px;
  padding: 2px 8px; border-radius: 4px; font-weight: 500; font-size: 13px;
}
.num-positive { color: #e74c3c !important }
.num-negative { color: #27ae60 !important }
.num-warning { color: #f39c12 }
.change-badge.num-positive { background: #fde8e8 }
.change-badge.num-negative { background: #e8f5e9 }

.lead-name { margin-right: 8px; font-weight: 500 }
.lead-code { font-size: 12px }

.ss-desc {
  padding: 8px 12px; margin-bottom: 12px;
  background: #f0f9ff; border-radius: 4px; color: #606266; font-size: 13px;
}
</style>
