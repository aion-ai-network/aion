<script setup lang="ts">
import { useSystemStore } from './store'
import type { CheckState } from './store'

const system = useSystemStore()
const components: Record<string, string> = {
  database: 'База данных', schema: 'Схема данных', redis: 'Очередь',
  scheduler: 'Планировщик', worker: 'Фоновая обработка',
}
const labels: Record<CheckState, string> = {
  up: 'Доступно', down: 'Недоступно', starting: 'Ожидаем запуск', unknown: 'Не проверено',
}
</script>

<template>
  <section class="page status-page">
    <p class="eyebrow">ПОДКЛЮЧЕНИЕ</p>
    <h1>Состояние сервиса</h1>
    <p class="lead">Текущая доступность AION и фоновой обработки.</p>
    <div v-if="system.error" class="notice" role="alert">{{ system.error }}</div>
    <div class="status-card" aria-live="polite" :aria-busy="system.loading">
      <div class="status-summary">
        <strong>{{ system.status?.operational ? 'Всё готово' : system.status?.ready ? 'API доступен, ожидаем фоновые процессы' : system.loading ? 'Проверяем подключение…' : 'Сервис пока не готов' }}</strong>
        <span v-if="system.status" class="version">v{{ system.status.version }}</span>
      </div>
      <dl>
        <div v-for="(label, key) in components" :key="key" class="status-row">
          <dt>{{ label }}</dt>
          <dd :class="system.status?.checks[key]?.status ?? 'unknown'">
            {{ labels[system.status?.checks[key]?.status ?? 'unknown'] }}
          </dd>
        </div>
      </dl>
    </div>
    <div class="status-actions">
      <button type="button" :disabled="system.loading" @click="system.refresh">
        {{ system.loading ? 'Проверяем…' : 'Проверить ещё раз' }}
      </button>
      <span v-if="system.checkedAt" class="quiet-note">
        Проверено в {{ system.checkedAt.toLocaleTimeString('ru-RU') }}
      </span>
    </div>
  </section>
</template>
