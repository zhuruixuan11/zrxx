<template>
  <div class="datasets-page">
    <div class="page-header">
      <h2>📚 数据集（衡策智算的知识源）</h2>
      <div class="header-actions">
        <el-button @click="loadAll" :icon="Refresh">刷新</el-button>
        <el-button type="info" plain @click="onSeed" :icon="MagicStick">一键种子数据</el-button>
        <el-button type="primary" @click="openCreate" :icon="Plus">新建数据集</el-button>
      </div>
    </div>

    <el-alert
      type="info"
      show-icon
      :closable="false"
      title="💡 想让 Agent 吸收你的知识？把数据集「发布」即可。发布后，衡策智算对话时会自动从这些数据集里检索知识（RAG）。"
      style="margin-bottom: 16px"
    />

    <el-tabs v-model="activeTab" type="border-card" class="main-tabs">
      <!-- 列表 -->
      <el-tab-pane label="数据集列表" name="list">
        <el-table :data="datasets" stripe style="width: 100%" v-loading="loading">
          <el-table-column prop="name" label="名称" min-width="180" />
          <el-table-column prop="type" label="类型" width="120">
            <template #default="{ row }">
              <el-tag :type="typeTag(row.type)">{{ row.typeLabel }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="sample_count" label="样本" width="80" align="center" />
          <el-table-column label="发布给 Agent" width="180">
            <template #default="{ row }">
              <el-switch
                v-model="row.is_published"
                active-text="已发布"
                inactive-text="未发布"
                inline-prompt
                @change="onTogglePublish(row)"
              />
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="primary" link @click="openDetail(row)">查看/编辑</el-button>
              <el-button size="small" type="danger" link @click="onDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 详情 -->
      <el-tab-pane v-if="currentDataset" :label="`${currentDataset.name} — 样本管理`" name="detail">
        <div class="detail-bar">
          <el-tag :type="typeTag(currentDataset.type)">{{ typeLabel(currentDataset.type) }} · {{ currentDataset.sample_count }} 条</el-tag>
          <el-tag v-if="currentDataset.is_published" type="success" effect="dark">✅ 已发布给 Agent</el-tag>
          <el-tag v-else type="info" effect="plain">未发布</el-tag>
          <div style="flex:1" />
          <el-upload :auto-upload="false" :show-file-list="false" :on-change="onFileSelected" accept=".json,.jsonl">
            <el-button :icon="Upload">导入 JSONL/JSON</el-button>
          </el-upload>
          <el-button type="primary" :icon="Plus" @click="openAddSample">新增样本</el-button>
        </div>

        <el-table :data="samples" stripe style="width: 100%" v-loading="samplesLoading">
          <el-table-column type="index" label="#" width="50" />
          <el-table-column label="内容预览" min-width="400">
            <template #default="{ row }">
              <!-- raw_knowledge / topic + content -->
              <div v-if="row.topic || row.content" class="preview-box">
                <div v-if="row.topic" class="preview-line"><el-tag size="small" type="warning" style="margin-right:6px">主题</el-tag>{{ truncate(row.topic, 60) }}</div>
                <div v-if="row.content" class="preview-line"><el-tag size="small" type="success" style="margin-right:6px">内容</el-tag>{{ truncate(row.content, 100) }}</div>
              </div>
              <!-- ChatML messages -->
              <div v-else-if="row.messages" class="preview-box">
                <div v-for="(m, i) in row.messages" :key="i" class="preview-line">
                  <el-tag size="small" :type="m.role === 'user' ? '' : m.role === 'assistant' ? 'success' : 'info'" style="margin-right:6px">{{ m.role }}</el-tag>
                  <span>{{ truncate(m.content, 60) }}</span>
                </div>
              </div>
              <!-- DPO -->
              <div v-else-if="currentDataset.type === 'dpo'" class="preview-box">
                <div class="preview-line"><el-tag size="small" type="info" style="margin-right:6px">prompt</el-tag>{{ truncate(row.prompt, 50) }}</div>
                <div class="preview-line"><el-tag size="small" type="success" style="margin-right:6px">chosen</el-tag>{{ truncate(row.chosen, 50) }}</div>
              </div>
              <!-- SFT plain -->
              <div v-else class="preview-box">
                <div v-if="row.instruction" class="preview-line"><el-tag size="small" style="margin-right:6px">inst</el-tag>{{ truncate(row.instruction, 50) }}</div>
                <div v-if="row.output" class="preview-line"><el-tag size="small" type="success" style="margin-right:6px">out</el-tag>{{ truncate(row.output, 50) }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="danger" link @click="onDeleteSample(row)">删</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination-bar">
          <el-pagination
            v-model:current-page="samplePage"
            v-model:page-size="sampleSize"
            :total="samplesTotal"
            :page-sizes="[10, 20, 50]"
            layout="total, sizes, prev, pager, next"
            @current-change="loadSamples"
            @size-change="loadSamples"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 新建 Dialog -->
    <el-dialog v-model="createVisible" title="新建数据集" width="500px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="名称"><el-input v-model="createForm.name" placeholder="例如：我的行业研究" /></el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="createForm.type">
            <el-radio value="sft">SFT（指令微调）</el-radio>
            <el-radio value="dpo">DPO（偏好优化）</el-radio>
            <el-radio value="raw_knowledge">Raw Knowledge（纯知识，直接喂 Agent）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="描述"><el-input v-model="createForm.description" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 新增样本 Dialog -->
    <el-dialog v-model="addSampleVisible" title="新增样本" width="560px">
      <el-form :model="sampleForm" label-width="80px">
        <template v-if="currentDataset?.type === 'raw_knowledge'">
          <el-form-item label="主题"><el-input v-model="sampleForm.topic" placeholder="例如：北向资金动向" /></el-form-item>
          <el-form-item label="内容"><el-input v-model="sampleForm.content" type="textarea" :rows="4" placeholder="具体知识/解释/数据" /></el-form-item>
        </template>
        <template v-else-if="currentDataset?.type === 'dpo'">
          <el-form-item label="Prompt"><el-input v-model="sampleForm.prompt" type="textarea" :rows="2" /></el-form-item>
          <el-form-item label="Chosen（好回答）"><el-input v-model="sampleForm.chosen" type="textarea" :rows="3" /></el-form-item>
          <el-form-item label="Rejected（差回答）"><el-input v-model="sampleForm.rejected" type="textarea" :rows="3" /></el-form-item>
        </template>
        <template v-else>
          <el-form-item label="Instruction"><el-input v-model="sampleForm.instruction" type="textarea" :rows="2" /></el-form-item>
          <el-form-item label="Output（期望回答）"><el-input v-model="sampleForm.output" type="textarea" :rows="4" /></el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="addSampleVisible = false">取消</el-button>
        <el-button type="primary" @click="onAddSample">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Upload, MagicStick } from '@element-plus/icons-vue'
import { datasetsApi, type Dataset, type DatasetSample } from '@/api/datasets'

const activeTab = ref('list')
const loading = ref(false)
const datasets = ref<(Dataset & { typeLabel: string })[]>([])

const currentDataset = ref<Dataset | null>(null)
const samples = ref<DatasetSample[]>([])
const samplesLoading = ref(false)
const samplePage = ref(1)
const sampleSize = ref(20)
const samplesTotal = ref(0)

const createVisible = ref(false)
const createForm = reactive({ name: '', description: '', type: 'sft' as 'sft' | 'dpo' | 'raw_knowledge' })

const addSampleVisible = ref(false)
const sampleForm = reactive({
  instruction: '', input_text: '', output: '',
  prompt: '', chosen: '', rejected: '',
  topic: '', content: '',
})

function truncate(s: string | null | undefined, n: number) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

function typeTag(t: string) {
  return { sft: 'primary', dpo: 'warning', raw_knowledge: 'success' }[t] || 'info'
}

function typeLabel(t: string) {
  return { sft: 'SFT', dpo: 'DPO', raw_knowledge: '知识条目' }[t] || t
}

async function loadAll() {
  loading.value = true
  try {
    const raw = await datasetsApi.list()
    datasets.value = raw.map((d): Dataset & { typeLabel: string } => ({
      ...d,
      typeLabel: { sft: 'SFT', dpo: 'DPO', raw_knowledge: '知识条目' }[d.type] || d.type,
    }))
  } finally {
    loading.value = false
  }
}

async function openCreate() {
  Object.assign(createForm, { name: '', description: '', type: 'sft' })
  createVisible.value = true
}

async function onCreate() {
  if (!createForm.name.trim()) return ElMessage.warning('请填名称')
  await datasetsApi.create(createForm)
  ElMessage.success('已创建')
  createVisible.value = false
  await loadAll()
}

async function onDelete(row: Dataset) {
  await ElMessageBox.confirm(`确认删除「${row.name}」及所有样本？`, '警告', { type: 'warning' })
  await datasetsApi.remove(row.id)
  ElMessage.success('已删除')
  await loadAll()
}

async function onSeed() {
  await datasetsApi.seed()
  ElMessage.success('已初始化 SFT + DPO + 知识条目三个种子集，知识条目已自动发布！')
  await loadAll()
}

async function onTogglePublish(row: Dataset & { is_published: boolean }) {
  const prev = row.is_published
  try {
    await datasetsApi.publish(row.id, row.is_published)
    ElMessage.success(row.is_published ? '✅ 已发布给 Agent，下次对话自动吸收' : '已取消发布')
    await loadAll()
  } catch (e: any) {
    row.is_published = prev
    ElMessage.error(e?.message || '操作失败')
  }
}

function openDetail(row: Dataset) {
  currentDataset.value = row
  samplePage.value = 1
  activeTab.value = 'detail'
  loadSamples()
}

async function loadSamples() {
  if (!currentDataset.value) return
  samplesLoading.value = true
  try {
    const r = await datasetsApi.samples(currentDataset.value.id, samplePage.value, sampleSize.value)
    samples.value = r.list
    samplesTotal.value = r.total
  } finally {
    samplesLoading.value = false
  }
}

function openAddSample() {
  Object.assign(sampleForm, {
    instruction: '', input_text: '', output: '',
    prompt: '', chosen: '', rejected: '',
    topic: '', content: '',
  })
  addSampleVisible.value = true
}

async function onAddSample() {
  if (!currentDataset.value) return
  const hasAny = currentDataset.value.type === 'raw_knowledge'
    ? (sampleForm.topic || sampleForm.content)
    : currentDataset.value.type === 'dpo'
      ? (sampleForm.prompt && sampleForm.chosen)
      : (sampleForm.instruction || sampleForm.output)
  if (!hasAny) return ElMessage.warning('请至少填写一项内容')

  await datasetsApi.addSample(currentDataset.value.id, sampleForm as any)
  ElMessage.success('样本已添加')
  addSampleVisible.value = false
  await loadSamples()
  await loadAll()
}

async function onDeleteSample(row: DatasetSample) {
  if (!currentDataset.value) return
  await ElMessageBox.confirm('确认删除该样本？', '警告', { type: 'warning' })
  await datasetsApi.removeSample(currentDataset.value.id, row.id)
  ElMessage.success('已删除')
  await loadSamples()
  await loadAll()
}

async function onFileSelected(file: File) {
  if (!currentDataset.value) return
  try {
    const r = await datasetsApi.upload(currentDataset.value.id, file)
    ElMessage.success(`导入 ${r.created} 条样本（${r.filename}）`)
    await loadSamples()
    await loadAll()
  } catch (e: any) {
    ElMessage.error(e?.message || '导入失败')
  }
}

onMounted(loadAll)
</script>

<style scoped>
.datasets-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.page-header h2 { margin: 0; }
.header-actions { display: flex; gap: 8px; }
.main-tabs { background: #fff; }
.detail-bar { display: flex; align-items: center; gap: 10px; padding: 12px 0; flex-wrap: wrap; }
.preview-box { display: flex; flex-direction: column; gap: 3px; }
.preview-line { font-size: 13px; color: #555; }
.pagination-bar { display: flex; justify-content: flex-end; padding: 12px 0; }
</style>
