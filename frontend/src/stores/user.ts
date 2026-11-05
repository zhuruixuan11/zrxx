/** Pinia 用户 Store —— token 持久化 + 用户信息 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User } from '@/types'
import { authApi } from '@/api/auth'

const TOKEN_KEY = 'hengce_token'
const USER_KEY = 'hengce_user'

export const useUserStore = defineStore('user', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<User | null>(
    JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
  )

  const isLoggedIn = computed(() => !!token.value)

  function setAuth(newToken: string, newUser: User) {
    token.value = newToken
    user.value = newUser
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(USER_KEY, JSON.stringify(newUser))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  async function fetchMe() {
    try {
      const u = await authApi.me()
      user.value = u
      localStorage.setItem(USER_KEY, JSON.stringify(u))
      return u
    } catch {
      logout()
      return null
    }
  }

  return { token, user, isLoggedIn, setAuth, logout, fetchMe }
})
