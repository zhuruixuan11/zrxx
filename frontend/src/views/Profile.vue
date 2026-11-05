<template>
  <div class="profile-page">
    <el-card shadow="hover">
      <template #header>
        <div class="card-head">
          <span class="card-title">
            <el-icon style="margin-right: 6px"><User /></el-icon>
            我的投资画像
          </span>
          <el-button type="primary" :loading="saving" @click="save">
            <el-icon><Check /></el-icon>
            保存画像
          </el-button>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="画像会注入到「衡策智算」的对话上下文，用来调整建议的仓位、标的范围与风险提示。"
        style="margin-bottom: 18px"
      />

      <el-form :model="form" label-width="100px" style="max-width: 720px">
        <el-form-item label="风险偏好">
          <el-radio-group v-model="form.risk_level">
            <el-radio-button v-for="r in options.risk_levels" :key="r.value" :value="r.value">
              {{ r.label }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="投资期限">
          <el-select v-model="form.investment_horizon" placeholder="选择" style="width: 220px" clearable>
            <el-option v-for="h in options.horizons" :key="h.value" :label="h.label" :value="h.value" />
          </el-select>
        </el-form-item>

        <el-form-item label="偏好板块">
          <el-select
            v-model="form.preferred_sectors"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车，如：半导体、新能源、医药"
            style="width: 100%"
          >
            <el-option v-for="s in SECTOR_PRESETS" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>

        <el-form-item label="回避板块">
          <el-select
            v-model="form.avoided_sectors"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车，如：地产、ST、白酒"
            style="width: 100%"
          >
            <el-option v-for="s in SECTOR_PRESETS" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>

        <el-form-item label="投资风格">
          <el-input
            v-model="form.investment_style"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="例如：偏好低估值蓝筹，习惯左侧分批建仓，不追高，严格止损"
          />
        </el-form-item>

        <el-form-item label="个人简介">
          <el-input
            v-model="form.bio"
            type="textarea"
            :rows="2"
            maxlength="500"
            show-word-limit
            placeholder="选填，如：入市 5 年，主要做中长线"
          />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="hover" style="margin-top: 16px">
      <template #header>
        <span class="card-title">
          <el-icon style="margin-right: 6px"><Document /></el-icon>
          智算看到的画像（实时预览）
        </span>
      </template>
      <pre v-if="previewText" class="preview">{{ previewText }}</pre>
      <el-empty v-else description="填写上面任意一项后，这里会显示注入给 AI 的内容" :image-size="70" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Document, User } from '@element-plus/icons-vue'
import { profileApi, type ProfileOptions } from '@/api/profile'

const SECTOR_PRESETS = [
  '半导体', '新能源', '医药', '白酒', '银行', '券商', '军工',
  '人工智能', '消费电子', '光伏', '汽车', '地产', '煤炭', '有色金属',
]

const saving = ref(false)
const summary = ref('')
const options = reactive<ProfileOptions>({
  risk_levels: [
    { value: 'conservative', label: '保守' },
    { value: 'moderate', label: '稳健' },
    { value: 'balanced', label: '平衡' },
    { value: 'aggressive', label: '积极' },
  ],
  horizons: [
    { value: 'short', label: '短线' },
    { value: 'medium', label: '中期' },
    { value: 'long', label: '长线' },
  ],
})

const form = reactive({
  risk_level: 'moderate',
  investment_horizon: '',
  preferred_sectors: [] as string[],
  avoided_sectors: [] as string[],
  investment_style: '',
  bio: '',
})

/** 本地按当前表单即时拼出摘要，避免每次改动都打后端 */
const previewText = computed(() => {
  const riskLabel = options.risk_levels.find(r => r.value === form.risk_level)?.label
  const horizonLabel = options.horizons.find(h => h.value === form.investment_horizon)?.label
  const lines: string[] = []
  if (riskLabel) lines.push(`- 风险偏好：${riskLabel}`)
  if (horizonLabel) lines.push(`- 投资期限：${horizonLabel}`)
  if (form.preferred_sectors.length) lines.push(`- 偏好板块：${form.preferred_sectors.join('、')}`)
  if (form.avoided_sectors.length) lines.push(`- 回避板块：${form.avoided_sectors.join('、')}`)
  if (form.investment_style.trim()) lines.push(`- 投资风格：${form.investment_style.trim()}`)
  if (!lines.length) return ''
  return `【用户投资画像】\n${lines.join('\n')}\n请在建议的仓位大小、标的范围、风险提示上贴合该画像。`
})

async function load() {
  try {
    const res = await profileApi.get()
    form.risk_level = res.profile.risk_level || 'moderate'
    form.investment_horizon = res.profile.investment_horizon || ''
    form.preferred_sectors = res.profile.preferred_sectors || []
    form.avoided_sectors = res.profile.avoided_sectors || []
    form.investment_style = res.profile.investment_style || ''
    form.bio = res.profile.bio || ''
    summary.value = res.summary
    if (res.options) {
      options.risk_levels = res.options.risk_levels
      options.horizons = res.options.horizons
    }
  } catch {
    /* 拦截器已提示 */
  }
}

async function save() {
  saving.value = true
  try {
    const res = await profileApi.update({
      risk_level: form.risk_level,
      investment_horizon: form.investment_horizon,
      preferred_sectors: form.preferred_sectors,
      avoided_sectors: form.avoided_sectors,
      investment_style: form.investment_style,
      bio: form.bio,
    })
    summary.value = res.summary
    ElMessage.success('画像已保存，智算下轮对话就会用上')
  } finally {
    saving.value = false
  }
}

onMounted(load)
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
.preview {
  background: #f6f8fa;
  border-radius: 6px;
  padding: 14px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  color: #303133;
  margin: 0;
  font-family: inherit;
}
</style>
