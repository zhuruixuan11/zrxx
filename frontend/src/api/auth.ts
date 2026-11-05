/** 认证 API —— 每个后端路由映射为命名方法 */
import request from './request'
import type { User } from '@/types'

interface LoginResult {
  token: string
  token_type: string
  user: User
}

interface RegisterResult extends LoginResult {}

export const authApi = {
  /** 注册并自动登录 */
  register(username: string, email: string, password: string, riskLevel = 'moderate') {
    return request.post<RegisterResult>('/api/auth/register', {
      username, email, password, risk_level: riskLevel,
    })
  },

  /** 登录（后端用 OAuth2PasswordRequestForm，用 form-urlencoded） */
  login(username: string, password: string) {
    const form = new URLSearchParams()
    form.append('username', username)
    form.append('password', password)
    return request.post<LoginResult>('/api/auth/login', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
  },

  /** 获取当前用户信息 */
  me() {
    return request.get<User>('/api/auth/me')
  },
}
