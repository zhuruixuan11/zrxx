<template>
  <div class="watch-page">
    <!-- 新增关注 -->
    <el-card shadow="hover">
      <template #header>
        <div class="card-head">
          <span class="card-title">
            <el-icon style="margin-right: 6px"><Bell /></el-icon>
            持仓跟随监控
          </span>
          <div class="head-right">
            <el-tag size="small" type="info">复用 60 秒轮询</el-tag>
            <el-tag size="small" :type="overview.monitor.email_configured ? 'success' : 'warning'">
              邮件{{ overview.monitor.email_configured ? '已配置' : '未配置' }}
            </el-tag>
          </div>
        </div>
      </template>

      <el-form :model="form" label-width="90px" inline @submit.prevent>
        <el-form-item label="关注标的">
          <el-input v-model="form.symbol" placeholder="6 位代码，如 600519" style="width: 180px" maxlength="6" />
        </el-form-item>
        <el-form-item label="异动阈值">
          <el-input-number v-model="form.price_threshold_pct" :min="0.5" :max="20" :step="0.5" style="width: 130px" />
          <span class="unit">%</span>
        </el-form-item>
        <el-form-item label="监控项">
          <el-checkbox v-model="form.watch_price">价格异动</el-checkbox>
          <el-checkbox v-model="form.watch_holding">持仓变动</el-checkbox>
          <el-checkbox v-model="form.watch_announcement">新公告</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="addItem">
            <el-icon><Plus /></el-icon>
            添加关注
          </el-button>
          <el-button :loading="checking" @click="checkNow">
            <el-icon><Refresh /></el-icon>
            立即检查
          </el-button>
        </el-form-item>
      </el-form>

      <div class="hint">
        <el-icon><InfoFilled /></el-icon>
        价格异动按「|现价 / 昨收 - 1| ≥ 阈值」判定（仅交易时段）；持仓变动只统计你自己组合里的数量变化；
        新公告来自东财全市场公告，30 分钟拉取一次。同一事件只推送一次。
      </div>
    </el-card>

    <!-- 关注列表 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div class="card-head">
          <span class="card-title">关注列表（{{ overview.items.length }}）</span>
          <span class="muted">
            {{ overview.monitor.is_trading_now ? '交易时段' : '非交易时段' }} ·
            每 {{ overview.monitor.poll_interval_seconds }}s 检查
          </span>
        </div>
      </template>

      <el-table v-if="overview.items.length" :data="overview.items" size="small" stripe>
        <el-table-column label="标的" min-width="140">
          <template #default="{ row }">
            <span class="symbol">{{ row.symbol }}</span>
            <span class="name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="监控项" min-width="200">
          <template #default="{ row }">
            <el-tag v-if="row.watch_price" size="small" type="warning" class="tag">
              价格 ≥ {{ row.price_threshold_pct }}%
            </el-tag>
            <el-tag v-if="row.watch_holding" size="small" type="info" class="tag">持仓变动</el-tag>
            <el-tag v-if="row.watch_announcement" size="small" type="success" class="tag">新公告</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="持仓基线" width="130" class-name="num">
          <template #default="{ row }">
            {{ row.baseline_quantity != null ? `${row.baseline_quantity} 股` : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="最新价" width="100" class-name="num">
          <template #default="{ row }">
            {{ row.last_price != null ? row.last_price.toFixed(2) : '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="last_checked_at" label="最近检查" width="160" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              size="small"
              @change="(v: boolean) => toggle(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="还没有关注标的，先在上方添加" :image-size="70" />
    </el-card>

    <!-- 事件流 -->
    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <div class="card-head">
          <span class="card-title">事件记录（已去重）</span>
          <span class="muted">同类型同事件只出现一次</span>
        </div>
      </template>

      <el-table v-if="overview.events.length" :data="overview.events" size="small" stripe>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="typeTag(row.event_type)">{{ typeLabel(row.event_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="代码" width="90" />
        <el-table-column prop="title" label="事件" min-width="260" show-overflow-tooltip />
        <el-table-column label="邮件" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.notified ? 'success' : 'info'">
              {{ row.notified ? '已发送' : '未发送' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="160" />
        <el-table-column label="详情" width="80">
          <template #default="{ row }">
            <el-popover :width="420" trigger="click">
              <template #reference>
                <el-button link type="primary" size="small">查看</el-button>
              </template>
              <div class="detail">{{ row.detail }}</div>
            </el-popover>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无事件" :image-size="70" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Bell, InfoFilled, Plus, Refresh } from '@element-plus/icons-vue'
import { watchApi, type WatchEventType, type WatchItem, type WatchOverview } from '@/api/watch'

const creating = ref(false)
const checking = ref(false)
const overview = ref<WatchOverview>({
  items: [],
  events: [],
  monitor: {
    enabled: false,
    poll_interval_seconds: 60,
    is_trading_now: false,
    email_configured: false,
    server_time: '',
  },
})

const form = reactive({
  symbol: '',
  price_threshold_pct: 5,
  watch_price: true,
  watch_holding: true,
  watch_announcement: true,
})

onMounted(load)

async function load() {
  try {
    overview.value = await watchApi.list()
  } catch { /* 拦截器已提示 */ }
}

async function addItem() {
  if (!/^\d{6}$/.test(form.symbol)) {
    ElMessage.warning('请输入 6 位股票代码')
    return
  }
  creating.value = true
  try {
    await watchApi.create({ ...form })
    ElMessage.success('已添加关注')
    form.symbol = ''
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    creating.value = false
  }
}

async function toggle(row: WatchItem, enabled: boolean) {
  try {
    await watchApi.update(row.id, { enabled })
    row.enabled = enabled
  } catch { /* 拦截器已提示 */ }
}

async function removeItem(row: WatchItem) {
  try {
    await ElMessageBox.confirm(`确定取消关注 ${row.name}（${row.symbol}）？`, '取消关注', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await watchApi.remove(row.id)
    ElMessage.success('已取消关注')
    await load()
  } catch { /* 拦截器已提示 */ }
}

async function checkNow() {
  checking.value = true
  try {
    const res = await watchApi.check(true)
    if (res.created) {
      ElMessage.success(`本轮新增 ${res.created} 条事件${res.email_configured ? '' : '（SMTP 未配置，仅站内记录）'}`)
    } else {
      ElMessage.info(`已检查 ${res.checked} 项，没有新事件`)
    }
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    checking.value = false
  }
}

const TYPE_LABELS: Record<WatchEventType, string> = {
  price: '价格异动',
  holding: '持仓变动',
  announcement: '新公告',
}
const TYPE_TAGS: Record<WatchEventType, 'warning' | 'info' | 'success'> = {
  price: 'warning',
  holding: 'info',
  announcement: 'success',
}

function typeLabel(t: WatchEventType) {
  return TYPE_LABELS[t] || t
}

function typeTag(t: WatchEventType) {
  return TYPE_TAGS[t] || 'info'
}
</script>

<style scoped>
.watch-page {
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
.head-right {
  display: flex;
  gap: 8px;
}
.hint {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: #7a8896;
  font-size: 12px;
  line-height: 1.7;
  margin-top: 4px;
}
.muted {
  color: #8a97a5;
  font-size: 12px;
}
.unit {
  margin-left: 4px;
  color: #8a97a5;
}
.symbol {
  font-variant-numeric: tabular-nums;
  color: #20303f;
}
.name {
  margin-left: 8px;
  color: #7a8896;
}
.tag {
  margin-right: 4px;
}
.num {
  font-variant-numeric: tabular-nums;
}
.detail {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.7;
  color: #20303f;
}
</style>
