/**
 * 单一 API 入口 —— axios 实例 + 拦截器
 * 遵循经验教训：拦截器统一注入 token、统一错误处理、统一返回格式
 */
import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

// 创建实例，但先声明返回类型为 any（拦截器会解包）
const instance = axios.create({
  baseURL: '',
  timeout: 30000,   // baostock 首次 login ~20s，留足余量
}) as unknown as AxiosInstance

// 请求拦截器：注入 Bearer token
instance.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('hengce_token')
    if (token && config.url?.startsWith('/api/')) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (err) => Promise.reject(err),
)

// 响应拦截器：统一解包 {code, msg, data} + 错误处理
instance.interceptors.response.use(
  (resp) => {
    const body = resp.data
    if (body?.code === 0) {
      return body.data
    }
    ElMessage.error(body?.msg || '请求失败')
    return Promise.reject(new Error(body?.msg || '请求失败'))
  },
  (err) => {
    const status = err.response?.status
    // 后端统一返回 {code,msg}；FastAPI 原生错误是 {detail}；slowapi 限流是 {error}
    const data = err.response?.data
    let msg: string = data?.msg || data?.detail || data?.error || err.message
    if (status === 429) {
      msg = '请求太频繁了，请稍等片刻再试'
    }

    if (status === 401) {
      localStorage.removeItem('hengce_token')
      localStorage.removeItem('hengce_user')
      if (router.currentRoute.value.path !== '/login') {
        router.push('/login')
      }
      ElMessage.warning('登录已过期，请重新登录')
    } else {
      ElMessage.error(msg || `网络错误 (${status})`)
    }
    return Promise.reject(err)
  },
)

// 导出带泛型的 request 函数，返回 T 而不是 AxiosResponse<T>
export const request = {
  get<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return instance.get(url, config) as unknown as Promise<T>
  },
  post<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return instance.post(url, data, config) as unknown as Promise<T>
  },
  put<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return instance.put(url, data, config) as unknown as Promise<T>
  },
  delete<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return instance.delete(url, config) as unknown as Promise<T>
  },
}

export default request
