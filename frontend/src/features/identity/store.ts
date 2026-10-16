import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api, ApiError, setCsrfToken } from '../../shared/api'

export interface Profile {
  user_id: string
  display_name: string
  timezone: string
  day_start: string
  day_end: string
  quiet_start: string
  quiet_end: string
  mode: 'normal' | 'light' | 'paused'
  notifications_enabled: boolean
  onboarding_completed: boolean
  version: number
}

export interface Account {
  user: { id: string; email: string; role: 'member' | 'admin' }
  profile: Profile
  limits: { ai_daily_requests: number; audio_daily_seconds: number; storage_limit_mb: number }
  csrf_token: string
  session_id: string
}

export const useAuthStore = defineStore('auth', () => {
  const account = ref<Account | null>(null)
  const initialized = ref(false)
  const error = ref<string | null>(null)

  function clear() {
    account.value = null
    setCsrfToken(null)
  }

  async function restore() {
    error.value = null
    try {
      account.value = await api<Account>('/api/v1/auth/me')
      setCsrfToken(account.value.csrf_token)
    } catch (cause) {
      clear()
      if (!(cause instanceof ApiError && cause.status === 401)) {
        error.value = cause instanceof Error ? cause.message : 'Не удалось проверить вход.'
      }
    } finally { initialized.value = true }
  }

  async function login(email: string, password: string) {
    await api('/api/v1/auth/login', { method: 'POST', body: { email, password } })
    await restore()
    if (!account.value) throw new Error(error.value ?? 'Не удалось открыть аккаунт.')
  }

  async function register(token: string, password: string, display_name: string) {
    await api('/api/v1/auth/register', { method: 'POST', body: { token, password, display_name } })
    await restore()
    if (!account.value) throw new Error(error.value ?? 'Не удалось открыть аккаунт.')
  }

  async function logout(all = false) {
    try {
      await api(`/api/v1/auth/${all ? 'logout-all' : 'logout'}`, { method: 'POST' })
    } catch (cause) {
      if (!(cause instanceof ApiError && cause.status === 401)) throw cause
    }
    clear()
  }

  return { account, initialized, error, restore, login, register, logout, clear }
})
