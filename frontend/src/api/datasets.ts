/** 数据集 API */
import request from './request'

export interface Dataset {
  id: string
  name: string
  description: string
  type: 'sft' | 'dpo' | 'raw_knowledge'
  sample_count: number
  is_published: boolean
  meta: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface DatasetSample {
  id: string
  topic: string | null
  content: string | null
  instruction: string | null
  input_text: string | null
  output: string | null
  prompt: string | null
  chosen: string | null
  rejected: string | null
  messages: Array<{ role: string; content: string }> | null
  quality: number
  note: string | null
  created_at: string
}

export const datasetsApi = {
  list(type?: string) {
    return request.get<Dataset[]>('/api/datasets', { params: type ? { type } : {} })
  },

  get(id: string) {
    return request.get<Dataset>(`/api/datasets/${id}`)
  },

  create(payload: { name: string; description?: string; type: 'sft' | 'dpo' | 'raw_knowledge' }) {
    return request.post<Dataset>('/api/datasets', payload)
  },

  update(id: string, payload: { name?: string; description?: string }) {
    return request.put<Dataset>(`/api/datasets/${id}`, payload)
  },

  remove(id: string) {
    return request.delete<null>(`/api/datasets/${id}`)
  },

  publish(id: string, publish: boolean) {
    return request.post<Dataset>(`/api/datasets/${id}/publish`, null, { params: { publish } })
  },

  samples(datasetId: string, page = 1, size = 20) {
    return request.get<{ list: DatasetSample[]; total: number; page: number; size: number }>(
      `/api/datasets/${datasetId}/samples`,
      { params: { page, size } },
    )
  },

  addSample(datasetId: string, payload: {
    instruction?: string
    input_text?: string
    output?: string
    prompt?: string
    chosen?: string
    rejected?: string
    topic?: string
    content?: string
    messages?: Array<{ role: string; content: string }>
  }) {
    return request.post<DatasetSample>(`/api/datasets/${datasetId}/samples`, payload)
  },

  removeSample(datasetId: string, sampleId: string) {
    return request.delete<null>(`/api/datasets/${datasetId}/samples/${sampleId}`)
  },

  upload(datasetId: string, file: File) {
    const fd = new FormData()
    fd.append('file', file)
    return request.post<{ created: number; filename: string }>(
      `/api/datasets/${datasetId}/upload`,
      fd,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
  },

  seed() {
    return request.post<{ seeded: boolean; hint?: string }>('/api/datasets/seed')
  },
}
