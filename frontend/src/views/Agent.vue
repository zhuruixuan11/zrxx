<template>
  <div class="agent-page">
    <el-card shadow="never" class="chat-card">
      <!-- 消息区 -->
      <div class="chat-area" ref="chatAreaRef">
        <div v-if="messages.length === 0" class="welcome">
          <el-icon :size="48" color="#1f4e79"><ChatDotRound /></el-icon>
          <h2>你好！我是 衡策智算</h2>
          <p>我可以帮你查看投资组合、查询行情、分析投资机会、解答理财知识。</p>
          <div class="quick-prompts">
            <el-button v-for="p in quickPrompts" :key="p" size="small" @click="send(p)">
              {{ p }}
            </el-button>
          </div>
        </div>

        <div v-for="(msg, idx) in messages" :key="idx" class="msg-row" :class="msg.role">
          <el-avatar :size="36" :style="avatarStyle(msg.role)">
            {{ msg.role === 'user' ? '我' : 'AI' }}
          </el-avatar>
          <div class="msg-body">
            <div v-if="msg.role === 'assistant' && msg.intent" class="intent-tag">
              <el-tag size="small" :type="intentTagType(msg.intent)">{{ intentLabel(msg.intent) }}</el-tag>
            </div>
            <div class="msg-content" v-if="msg.pending">
              <span class="dot"></span><span class="dot"></span><span class="dot"></span>
              <span class="typing-text">衡策智算正在思考…</span>
            </div>
            <div v-else class="msg-content" v-html="formatMsg(msg.content)" />

            <!-- ✨ 回测结果对比卡片（支持多标的） -->
            <div
              v-if="msg.backtest_results?.length"
              class="bt-card"
            >
              <div class="bt-header">
                <el-icon><DataAnalysis /></el-icon>
                <span>自动回测验证（120 交易日 · 20 万）</span>
                <span class="bt-sub">· 共 {{ msg.backtest_results.length }} 只标的</span>
              </div>
              <template v-for="(group, gi) in msg.backtest_results" :key="gi">
                <div class="bt-symbol-title">
                  <span class="bt-sym-name">{{ group.symbol_name || group.symbol }}</span>
                  <span class="bt-sym-code">{{ group.symbol }}</span>
                  <el-button
                    link type="primary" size="small"
                    @click="goBacktest(group.symbol)"
                  >完整回测 →</el-button>
                </div>
                <div class="bt-table">
                  <div class="bt-row bt-head">
                    <span>策略</span><span class="r">总收益</span>
                    <span class="r">年化</span><span class="r">Sharpe</span>
                    <span class="r">回撤</span><span class="r">交易</span>
                  </div>
                  <div
                    v-for="bt in group.results"
                    :key="bt.strategy_id"
                    class="bt-row"
                    :class="{ best: isBest(bt, group.results) }"
                  >
                    <span class="bt-name">{{ bt.strategy_name }}</span>
                    <span class="r" :class="bt.metrics.total_return_pct >= 0 ? 'pos' : 'neg'">
                      {{ bt.metrics.total_return_pct >= 0 ? '+' : '' }}{{ bt.metrics.total_return_pct.toFixed(2) }}%
                    </span>
                    <span class="r">{{ bt.metrics.annual_return_pct.toFixed(2) }}%</span>
                    <span class="r">{{ bt.metrics.sharpe_ratio.toFixed(2) }}</span>
                    <span class="r neg">{{ bt.metrics.max_drawdown_pct.toFixed(2) }}%</span>
                    <span class="r">{{ bt.metrics.trades_count }}</span>
                  </div>
                </div>
              </template>
              <!-- 跨标的横向对比 -->
              <div v-if="msg.backtest_results.length > 1" class="bt-compare">
                <div class="bt-compare-title">📊 跨标的最佳策略对比</div>
                <div class="bt-compare-row" v-for="group in msg.backtest_results" :key="group.symbol">
                  <span class="bt-compare-sym">{{ group.symbol_name || group.symbol }}</span>
                  <template v-if="bestInGroup(group)">
                    <span class="bt-compare-strat">{{ bestInGroup(group)!.strategy_name }}</span>
                    <span class="pos">
                      {{ bestInGroup(group)!.metrics.total_return_pct >= 0 ? '+' : '' }}
                      {{ bestInGroup(group)!.metrics.total_return_pct.toFixed(2) }}%
                    </span>
                    <span>Sharpe {{ bestInGroup(group)!.metrics.sharpe_ratio.toFixed(2) }}</span>
                  </template>
                  <span v-else class="bt-compare-empty">回测数据不足</span>
                </div>
              </div>
            </div>

            <div
              v-if="msg.role === 'assistant' && !msg.pending && msg.intent === 'investment_analysis' && msg.symbol"
              class="msg-actions"
            >
              <el-button link type="primary" size="small" @click="goBacktest(msg.symbol)">
                用回测验证这个策略 →
              </el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="input-area">
        <el-input
          ref="inputRef"
          v-model="input"
          type="textarea"
          :rows="2"
          placeholder="问我任何关于投资的问题..."
          @keydown.enter.exact.prevent="handleSend"
          resize="none"
        />
        <div class="input-actions">
          <span class="hint">
            Enter 发送 / Shift+Enter 换行
            <el-button v-if="messages.length" link size="small" @click="clearMessages">
              清空对话
            </el-button>
          </span>
          <el-button type="primary" :icon="Promotion" :loading="loading" @click="handleSend">
            发送
          </el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { Promotion, DataAnalysis } from '@element-plus/icons-vue'
import { agentApi } from '@/api/agent'

const router = useRouter()

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  intent?: string
  /** 分析类回答的股票代码（主代码） */
  symbol?: string
  /** 多标的分析的完整代码列表 */
  symbols?: string[]
  symbol_names?: string[]
  /** 按 symbol 分组的回测结果 */
  backtest_results?: Array<{
    symbol: string
    symbol_name: string
    results: Array<{
      strategy_id: string
      strategy_name: string
      metrics: {
        total_return_pct: number
        annual_return_pct: number
        sharpe_ratio: number
        max_drawdown_pct: number
        win_rate_pct: number
        trades_count: number
      }
    }>
  }>
  /** 等待回复时的占位气泡 */
  pending?: boolean
}

// 聊天记录本地持久化：切换页面/刷新后自动恢复
const STORAGE_KEY = 'hengce_chat_messages'

const messages = ref<ChatMsg[]>([])
const input = ref('')
const loading = ref(false)
const chatAreaRef = ref<HTMLDivElement>()
const inputRef = ref<{ focus: () => void }>()

function restoreMessages() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const list = JSON.parse(raw)
    if (!Array.isArray(list)) return
    messages.value = list
      .filter(
        (m: ChatMsg) =>
          m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string',
      )
      .slice(-60)
  } catch {
    localStorage.removeItem(STORAGE_KEY)
  }
}

function persistMessages() {
  try {
    // 只保存已完成的消息，避免刷新后残留「思考中」气泡
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(messages.value.filter(m => !m.pending).slice(-60)),
    )
  } catch {
    /* 本地存储不可用时忽略 */
  }
}

restoreMessages()
loadServerHistory()
nextTick(() => {
  scrollBottom()
  inputRef.value?.focus()
})

/** 本地没有记录时，从服务端恢复最近对话（换设备/清缓存后仍可续上，含回测卡片） */
async function loadServerHistory() {
  if (messages.value.length) return
  try {
    const data = await agentApi.history(40)
    if (!messages.value.length && data?.list?.length) {
      messages.value = data.list.map(m => ({
        role: m.role,
        content: m.content,
        intent: m.intent,
        symbol: m.symbols?.[0],
        symbols: m.symbols,
        symbol_names: m.symbol_names,
        backtest_results: m.backtest_results || [],
      }))
      persistMessages()
      scrollBottom()
    }
  } catch {
    /* 未登录或接口异常时保持空态 */
  }
}

const quickPrompts = [
  '我的投资组合怎么样？',
  'AAPL 今天行情如何？',
  '帮我分析一下现在适合买入什么',
  '什么是 ETF？',
]

async function send(text: string) {
  const content = text.trim()
  if (!content || loading.value) return
  const symbol = content.match(/\d{6}/)?.[0]

  messages.value.push({ role: 'user', content })
  input.value = ''
  loading.value = true

  // 先插入「思考中」占位，让用户立刻看到反馈
  const placeholder: ChatMsg = { role: 'assistant', content: '', pending: true }
  messages.value.push(placeholder)
  const replyIndex = messages.value.length - 1

  persistMessages()
  scrollBottom()

  try {
    const resp: any = await agentApi.chat(content)
    const r = resp.data || resp
    messages.value[replyIndex] = {
      role: 'assistant',
      content: r.response || resp.response || '',
      intent: r.intent || resp.intent,
      symbol: symbol || undefined,
      symbols: r.symbols || resp.symbols,
      symbol_names: r.symbol_names || resp.symbol_names,
      backtest_results: r.backtest_results || resp.backtest_results || [],
    }
  } catch (e) {
    const err = e as { code?: string; message?: string }
    const isTimeout = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
    messages.value[replyIndex] = {
      role: 'assistant',
      content: isTimeout
        ? '这次响应超时了（数据源或模型较慢）。可以稍后再试，或把问题问得更具体一些。'
        : '😢 抱歉，请求失败了。请稍后再试。',
    }
  } finally {
    loading.value = false
    persistMessages()
    scrollBottom()
    // 回复后自动聚焦输入框，可以直接接着输入
    nextTick(() => inputRef.value?.focus())
  }
}

function goBacktest(symbol?: string) {
  if (!symbol) return
  router.push({ path: '/backtest', query: { symbol } })
}

/** 在多策略回测里找 Sharpe 最高的（最值得关注的） */
function isBest(bt: any, all: any[]) {
  const positive = all.filter((x: any) => x.metrics.sharpe_ratio > 0)
  if (positive.length === 0) return false
  const bestSharpe = Math.max(...positive.map((x: any) => x.metrics.sharpe_ratio))
  return Math.abs(bt.metrics.sharpe_ratio - bestSharpe) < 0.001
}

/** 跨标的横向对比：取每组最佳策略 */
function bestInGroup(group: { results: any[] }) {
  const positive = group.results.filter(x => x.metrics.sharpe_ratio > 0)
  if (positive.length === 0) return null
  return positive.reduce((a, b) => a.metrics.sharpe_ratio > b.metrics.sharpe_ratio ? a : b)
}

function clearMessages() {
  messages.value = []
  localStorage.removeItem(STORAGE_KEY)
  // 同步清掉服务端记录，避免刷新后又把旧对话拉回来
  agentApi.clearHistory().catch(() => {})
}

function handleSend() {
  send(input.value)
}

async function scrollBottom() {
  await nextTick()
  if (chatAreaRef.value) {
    chatAreaRef.value.scrollTop = chatAreaRef.value.scrollHeight
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function formatMsg(text: string): string {
  // 先转义 HTML 再做 markdown-ish 渲染：AI 回复可能携带外部新闻内容或被诱导输出标记，防止 XSS
  return escapeHtml(text)
    .replace(/\n/g, '<br>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

function intentLabel(intent: string): string {
  const map: Record<string, string> = {
    portfolio_query: '投资组合',
    market_query: '行情查询',
    investment_analysis: '投资分析',
    knowledge_query: '知识问答',
    risk_assessment: '风险评估',
    alert_config: '预警设置',
    chitchat: '闲聊',
  }
  return map[intent] || intent
}

function intentTagType(intent: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  const map: Record<string, '' | 'success' | 'warning' | 'info' | 'danger'> = {
    portfolio_query: '',
    market_query: 'success',
    investment_analysis: 'warning',
    knowledge_query: 'info',
    risk_assessment: 'danger',
    alert_config: 'warning',
    chitchat: 'info',
  }
  return map[intent] || ''
}

function avatarStyle(role: string) {
  return role === 'user'
    ? { background: '#1f4e79' }
    : { background: '#2f6a9f' }
}
</script>

<style scoped>
.agent-page {
  height: calc(100vh - 56px - 40px);
  display: flex;
  flex-direction: column;
}
.chat-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 0;
}
.chat-card :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}
.chat-area {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.welcome {
  text-align: center;
  padding: 40px 20px;
  color: #606266;
}
.welcome h2 {
  margin: 12px 0 8px;
  font-size: 20px;
  color: #303133;
}
.welcome p {
  font-size: 14px;
  color: #909399;
  margin-bottom: 20px;
}
.quick-prompts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}
.msg-row {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.msg-row.user {
  flex-direction: row-reverse;
}
.msg-body {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 10px;
  line-height: 1.6;
  font-size: 14px;
}
.msg-row.user .msg-body {
  background: #1f4e79;
  color: #fff;
  border-top-right-radius: 2px;
}
.msg-row.assistant .msg-body {
  background: #f4f4f5;
  color: #303133;
  border-top-left-radius: 2px;
}
.intent-tag {
  margin-bottom: 6px;
}
.input-area {
  border-top: 1px solid #ebeef5;
  padding: 16px 20px;
  background: #fff;
}
.input-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}
.hint {
  font-size: 12px;
  color: #909399;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.msg-actions {
  margin-top: 4px;
}
.typing {
  display: flex;
  align-items: center;
}
.dot {
  width: 6px;
  height: 6px;
  margin-right: 3px;
  border-radius: 50%;
  background: #2f6a9f;
  animation: typing-blink 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
.typing-text {
  margin-left: 6px;
  font-size: 13px;
  color: #909399;
}
@keyframes typing-blink {
  0%, 80%, 100% { opacity: 0.25; }
  40% { opacity: 1; }
}

/* ✨ 回测结果对比卡片 */
.bt-card {
  margin-top: 12px; padding: 12px 14px;
  background: #f8fafc; border: 1px solid #e4e7ed; border-radius: 8px;
  font-size: 13px;
}
.bt-header {
  display: flex; align-items: center; gap: 6px;
  font-weight: 600; color: #303133; margin-bottom: 10px; font-size: 13px;
}
.bt-sub { font-size: 12px; color: #909399; font-weight: 400; }

.bt-symbol-title {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; margin: 8px 0 4px;
  background: #fff; border: 1px solid #ebeef5; border-radius: 6px;
}
.bt-symbol-title:first-of-type { margin-top: 0; }
.bt-sym-name { font-weight: 600; color: #1f4e79; font-size: 13px; }
.bt-sym-code { font-size: 11px; color: #909399; font-family: monospace; }

.bt-table {
  display: flex; flex-direction: column; gap: 4px;
}
.bt-row {
  display: grid; grid-template-columns: 1fr 70px 70px 60px 60px 48px;
  gap: 4px; padding: 4px 6px;
  border-radius: 4px;
}
.bt-row.bt-head {
  font-size: 11px; color: #909399; padding: 2px 6px;
}
.bt-row.best {
  background: linear-gradient(90deg, #ecfdf5, transparent 80%);
  border: 1px solid #a7f3d0;
}
.bt-row.best .bt-name::after {
  content: ' ★'; color: #10b981; font-size: 11px;
}
.bt-row .r { text-align: right; font-variant-numeric: tabular-nums; }
.bt-row .pos { color: #dc2626; font-weight: 500; }
.bt-row .neg { color: #16a34a; }
.bt-name { font-weight: 500; color: #303133; }

/* 跨标的横向对比 */
.bt-compare {
  margin-top: 12px; padding-top: 10px;
  border-top: 1px dashed #dcdfe6;
}
.bt-compare-title {
  font-size: 12px; font-weight: 600; color: #606266; margin-bottom: 6px;
}
.bt-compare-row {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 8px; margin-bottom: 3px;
  background: #fff; border-radius: 4px; font-size: 12px;
}
.bt-compare-sym { font-weight: 600; color: #1f4e79; min-width: 70px; }
.bt-compare-strat { color: #303133; }
.bt-compare-empty { color: #909399; font-style: italic; }

.bt-actions { margin-top: 8px; padding-top: 8px; border-top: 1px dashed #e4e7ed; }
</style>
