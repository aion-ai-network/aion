<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { useSystemStore } from '../features/system/store'

const system = useSystemStore()
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  void system.refresh()
  timer = setInterval(() => { void system.refresh() }, 30_000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <RouterLink to="/" class="brand" aria-label="AION — главная">
        <span class="brand-mark" aria-hidden="true">a</span><span>AION</span>
      </RouterLink>
      <p class="sidebar-caption">Меньше держать в голове.</p>
      <nav aria-label="Основная навигация">
        <RouterLink to="/" class="nav-link" exact-active-class="active">
          <span aria-hidden="true">◉</span> Сейчас
        </RouterLink>
      </nav>
      <div class="sidebar-footer">
        <RouterLink to="/status" class="connection" :class="{ connected: system.connected }">
          <span class="status-dot" aria-hidden="true"></span>
          {{ system.loading && !system.checkedAt ? 'Подключаемся' : system.connected ? 'Подключено' : 'Проверить подключение' }}
        </RouterLink>
      </div>
    </aside>
    <main id="main-content"><RouterView /></main>
  </div>
</template>
