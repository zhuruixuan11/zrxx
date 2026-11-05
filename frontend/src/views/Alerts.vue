<template>
  <div class="alerts-page">
    <!-- 新建预警 -->
    <el-card shadow="hover">
      <template #header>
        <div class="card-head">
          <span class="card-title">
            <el-icon style="margin-right: 6px"><Bell /></el-icon>
            到价预警
          </span>
          <div class="head-tags">
            <el-tag :type="monitor?.is_trading_now ? 'success' : 'info'" size="small">
              {{ monitor?.is_trading_now ? '交易时段 · 正在盯盘' : '非交易时段 · 暂停检查' }}
            </el-tag>
            <el-tag type="info" size="small">
              每 {{ monitor?.poll_interval_seconds ?? 60 }} 秒轮询
            </el-tag>
            <el-tag :type="monitor?.email_configured ? 'success' : 'info'" size="small">
              {{ monitor?.email_configured ? '邮件推送已开启' : '仅站内提醒' }}
            </el-tag>
            <el-button link type="primary" size="small" :loading="testingEmail" @click="testEmail">
              测试邮件
            </el-button>
          </div>
        </div>
      </template>

      <el-form :model="form" label-width="90px" inline>
        <el-form-item label="股票代码">
          <el-input
            v-model="form.symbol"
            placeholder="如 600519"
            style="width: 140px"
            maxlength="6"
          />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="可选" style="width: 120px" />
        </el-form-item>
        <el-form-item label="预警类型">
          <el-select v-model="form.alert_type" style="width: 130px">
            <el-option label="绝对价格" value="price" />
            <el-option label="涨跌幅" value="change" />
            <el-option label="止盈止损" value="cost" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.alert_type === 'cost'" label="成本价">
          <el-input-number
            v-model="form.reference_price"
            :min="0.01"
            :step="0.1"
            :precision="2"
            style="width: 140px"
          />
        </el-form-item>
        <el-form-item label="触发条件">
          <el-select v-model="form.operator" style="width: 90px">
            <el-option label="≥" value=">=" />
            <el-option label="≤" value="<=" />
            <el-option label=">" value=">" />
            <el-option label="<" value="<" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.alert_type === 'price'" label="目标价">
          <el-input-number v-model="form.threshold" :min="0.01" :step="0.1" :precision="2" style="width: 140px" />
        </el-form-item>
        <el-form-item v-else :label="form.alert_type === 'cost' ? '盈亏 %' : '涨跌幅 %'">
          <el-input-number v-model="form.change_pct" :min="-100" :max="100" :step="1" :precision="2" style="width: 140px" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.note" placeholder="可选，如：跌破支撑" style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="addAlert">
            <el-icon><Plus /></el-icon>
            添加预警
          </el-button>
        </el-form-item>
      </el-form>

      <div class="hint">
        <el-icon><InfoFilled /></el-icon>
        {{ hintText }}
      </div>
    </el-card>

    <!-- 监控中 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div class="card-head">
          <span class="card-title">监控中 <el-tag size="small" type="danger">{{ activeAlerts.length }}</el-tag></span>
          <div>
            <el-button size="small" :loading="checking" @click="checkNow">
              <el-icon><Refresh /></el-icon>
              立即检查
            </el-button>
            <el-button size="small" @click="load">
              <el-icon><RefreshRight /></el-icon>
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-table :data="activeAlerts" size="small" stripe v-if="activeAlerts.length">
        <el-table-column label="股票" min-width="140">
          <template #default="{ row }">
            <div class="stock-cell">
              <span class="stock-name">{{ row.name }}</span>
              <span class="stock-code">{{ row.symbol }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="触发条件" min-width="190">
          <template #default="{ row }">{{ row.condition }}</template>
        </el-table-column>
        <el-table-column label="现价" width="110" class-name="num">
          <template #default="{ row }">
            <span v-if="row.last_price != null">¥{{ row.last_price.toFixed(2) }}</span>
            <span v-else class="muted">待刷新</span>
          </template>
        </el-table-column>
        <el-table-column prop="last_checked_at" label="上次检查" width="160">
          <template #default="{ row }">
            <span class="muted">{{ row.last_checked_at || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="120">
          <template #default="{ row }">
            <span class="muted">{{ row.note || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="toggle(row)">停用</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="还没有监控中的预警，上面添加一条试试" :image-size="80" />
    </el-card>

    <!-- 已触发 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div class="card-head">
          <span class="card-title">触发记录 <el-tag size="small">{{ triggeredAlerts.length }}</el-tag></span>
        </div>
      </template>

      <el-table :data="triggeredAlerts" size="small" v-if="triggeredAlerts.length">
        <el-table-column label="股票" min-width="140">
          <template #default="{ row }">
            <div class="stock-cell">
              <span class="stock-name">{{ row.name }}</span>
              <span class="stock-code">{{ row.symbol }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="触发条件" min-width="190">
          <template #default="{ row }">{{ row.condition }}</template>
        </el-table-column>
        <el-table-column label="触发价" width="110" class-name="num">
          <template #default="{ row }">
            <span class="num-positive" v-if="row.triggered_price != null">
              ¥{{ row.triggered_price.toFixed(2) }}
            </span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column prop="triggered_at" label="触发时间" width="170" />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="rearm(row)">重新监控</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无触发记录" :image-size="80" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Bell, InfoFilled, Plus, Refresh, RefreshRight } from '@element-plus/icons-vue'
import { alertsApi, type AlertOperator, type AlertType, type MonitorInfo, type PriceAlert } from '@/api/alerts'

const alerts = ref<PriceAlert[]>([])
const monitor = ref<MonitorInfo | null>(null)
const creating = ref(false)
const checking = ref(false)
const testingEmail = ref(false)

const form = reactive({
  symbol: '',
  name: '',
  alert_type: 'price' as AlertType,
  operator: '>=' as AlertOperator,
  threshold: 10,
  reference_price: 10,
  change_pct: 5,
  note: '',
})

const hintText = computed(() => {
  if (form.alert_type === 'change') {
    return '涨跌幅按相对昨收计算；例如 ≥ +5% 表示当日涨幅达到 5% 时提醒，≤ -5% 表示跌幅达到 5% 时提醒。'
  }
  if (form.alert_type === 'cost') {
    return '止盈止损按相对成本价计算；例如 ≥ +10% 止盈、≤ -5% 止损。触发一次后自动停用。'
  }
  return '触发一次后自动停用，避免同一条件反复提醒。A 股需 6 位代码；港股/美股代码可能取不到实时价。'
})

const activeAlerts = computed(() => alerts.value.filter(a => a.enabled))
const triggeredAlerts = computed(() => alerts.value.filter(a => !a.enabled))

let timer: number | undefined

async function load() {
  try {
    const res = await alertsApi.list()
    alerts.value = res.items
    monitor.value = res.monitor
  } catch {
    /* 错误提示已由拦截器统一处理 */
  }
}

async function addAlert() {
  if (!/^\d{6}$/.test(form.symbol.trim())) {
    ElMessage.warning('请输入 6 位 A 股代码，如 600519')
    return
  }
  if (form.alert_type === 'cost' && form.reference_price <= 0) {
    ElMessage.warning('止盈止损需要填写成本价')
    return
  }
  creating.value = true
  try {
    await alertsApi.create({
      symbol: form.symbol.trim(),
      name: form.name.trim() || undefined,
      alert_type: form.alert_type,
      operator: form.operator,
      threshold: form.alert_type === 'price' ? form.threshold : undefined,
      reference_price: form.alert_type === 'cost' ? form.reference_price : undefined,
      change_pct: form.alert_type === 'price' ? undefined : form.change_pct,
      note: form.note.trim() || undefined,
    })
    ElMessage.success('预警已添加')
    form.symbol = ''
    form.name = ''
    form.note = ''
    await load()
  } finally {
    creating.value = false
  }
}

async function toggle(row: PriceAlert) {
  await alertsApi.update(row.id, { enabled: false })
  ElMessage.info('已停用该预警')
  await load()
}

async function rearm(row: PriceAlert) {
  await alertsApi.update(row.id, { enabled: true })
  ElMessage.success('已重新开始监控')
  await load()
}

async function remove(row: PriceAlert) {
  try {
    await ElMessageBox.confirm(`确定删除 ${row.name}（${row.symbol}）的预警？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await alertsApi.remove(row.id)
  ElMessage.success('已删除')
  await load()
}

async function checkNow() {
  checking.value = true
  try {
    const res = await alertsApi.checkNow()
    if (res.triggered > 0) {
      ElMessage.warning(`检查 ${res.checked} 条，触发 ${res.triggered} 条`)
    } else {
      ElMessage.success(`检查 ${res.checked} 条，暂未触发`)
    }
    await load()
  } finally {
    checking.value = false
  }
}

async function testEmail() {
  testingEmail.value = true
  try {
    const res = await alertsApi.testEmail()
    ElMessage.success(`测试邮件已发送至 ${res.sent_to}，请查收`)
  } catch {
    /* 失败原因由拦截器提示 */
  } finally {
    testingEmail.value = false
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 30_000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
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
  gap: 6px;
}
.head-tags {
  display: flex;
  gap: 8px;
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
.muted {
  color: #909399;
}
.hint {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
