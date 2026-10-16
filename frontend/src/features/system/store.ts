import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, getJson } from '../../shared/api'

export type CheckState = 'up' | 'down' | 'starting' | 'unknown'
export interface SystemStatus {
  service: 'aion'
  version: string
  ready: boolean
  operational: boolean
  checks: Record<string, { status: CheckState }>
}

export const useSystemStore = defineStore('system', () => {
  const status = ref<SystemStatus | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const checkedAt = ref<Date | null>(null)
  const connected = computed(() => status.value?.ready === true && error.value === null)

  async function refresh() {
    if (loading.value) return
    loading.value = true
    error.value = null
    try {
      status.value = await getJson<SystemStatus>('/api/v1/system/status')
    } catch (cause) {
      status.value = null
      error.value = cause instanceof ApiError ? cause.message : 'Не удалось проверить подключение.'
    } finally {
      checkedAt.value = new Date()
      loading.value = false
    }
  }

  return { status, loading, error, checkedAt, connected, refresh }
})
