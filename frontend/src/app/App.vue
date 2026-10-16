<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { useSystemStore } from '../features/system/store'
import { useAuthStore } from '../features/identity/store'

const system = useSystemStore()
const auth = useAuthStore()
const router = useRouter()
async function logout() {
  try { await auth.logout(); await router.replace('/login') }
  catch { await router.push('/settings') }
}
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
        <template v-if="auth.account">
          <RouterLink to="/chat" class="nav-link" active-class="active">Разговор</RouterLink>
          <RouterLink to="/knowledge" class="nav-link" active-class="active">Статьи</RouterLink>
          <RouterLink to="/memory" class="nav-link" active-class="active">Память</RouterLink>
          <RouterLink to="/notifications" class="nav-link" active-class="active">Уведомления</RouterLink>
        </template>
        <RouterLink v-if="auth.account" to="/settings" class="nav-link" active-class="active">Настройки</RouterLink>
        <RouterLink v-if="auth.account?.user.role === 'admin'" to="/access" class="nav-link" active-class="active">Доступ</RouterLink>
      </nav>
      <div class="sidebar-footer">
        <div v-if="auth.account" class="account-summary"><span>{{ auth.account.profile.display_name }}</span><button type="button" class="logout-button" @click="logout">Выйти</button></div>
        <RouterLink to="/status" class="connection" :class="{ connected: system.connected }">
          <span class="status-dot" aria-hidden="true"></span>
          {{ system.loading && !system.checkedAt ? 'Подключаемся' : system.connected ? 'Подключено' : 'Проверить подключение' }}
        </RouterLink>
      </div>
    </aside>
    <main id="main-content"><RouterView /></main>
  </div>
</template>
