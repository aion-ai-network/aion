<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../shared/api'
import { useAuthStore, type Profile } from './store'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const onboarding = computed(() => route.path === '/onboarding')
const original = auth.account!.profile
const form = reactive({
  display_name: original.display_name,
  timezone: original.onboarding_completed ? original.timezone : Intl.DateTimeFormat().resolvedOptions().timeZone,
  day_start: original.day_start.slice(0, 5), day_end: original.day_end.slice(0, 5),
  quiet_start: original.quiet_start.slice(0, 5), quiet_end: original.quiet_end.slice(0, 5),
  mode: original.mode, notifications_enabled: original.notifications_enabled,
  onboarding_completed: true, version: original.version,
})
const zones = Intl.supportedValuesOf('timeZone')
const busy = ref(false)
const error = ref('')
const saved = ref(false)
const sessions = ref<Array<{ id: string; created_at: string; expires_at: string }>>([])
async function loadSessions() {
  if (onboarding.value) return
  try { sessions.value = await api('/api/v1/auth/sessions') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось загрузить сессии.' }
}
onMounted(loadSessions)

async function save() {
  busy.value = true
  saved.value = false
  error.value = ''
  try {
    const profile = await api<Profile>(`/api/v1/profiles/${auth.account!.user.id}`, { method: 'PUT', body: form })
    auth.account!.profile = profile
    form.version = profile.version
    saved.value = true
    if (onboarding.value) await router.replace('/')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось сохранить настройки.' }
  finally { busy.value = false }
}
async function revoke(id: string) {
  error.value = ''
  try {
    await api(`/api/v1/auth/sessions/${id}`, { method: 'DELETE' })
    if (id === auth.account?.session_id) { auth.clear(); await router.replace('/login') }
    else await loadSessions()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось завершить сессию.' }
}
async function logoutAll() {
  try { await auth.logout(true); await router.replace('/login') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось завершить сессии.' }
}
</script>

<template>
  <section class="page profile-page">
    <p class="eyebrow">{{ onboarding ? 'ПОЗНАКОМИМСЯ' : 'ВАШИ НАСТРОЙКИ' }}</p>
    <h1>{{ onboarding ? 'Подстроим AION под вас' : 'Профиль и ритм' }}</h1>
    <p class="lead">{{ onboarding ? 'Несколько исходных настроек. Позже их можно изменить.' : auth.account?.user.email }}</p>
    <form class="form-card form-stack" @submit.prevent="save">
      <label>Как к вам обращаться<input v-model="form.display_name" autocomplete="nickname" required maxlength="80"></label>
      <label>Часовой пояс<input v-model="form.timezone" list="timezones" required maxlength="64"><datalist id="timezones"><option value="UTC"></option><option v-for="zone in zones" :key="zone" :value="zone"></option></datalist></label>
      <fieldset><legend>Примерное время для дел</legend><div class="form-grid"><label>Начало дня<input v-model="form.day_start" type="time" required></label><label>Конец дня<input v-model="form.day_end" type="time" required></label></div></fieldset>
      <label>Режим<select v-model="form.mode"><option value="normal">Обычный</option><option value="light">Лёгкий</option><option value="paused">Пауза</option></select></label>
      <label class="checkbox-label"><input v-model="form.notifications_enabled" type="checkbox">Разрешить напоминания</label>
      <fieldset><legend>Тихие часы</legend><div class="form-grid"><label>Не беспокоить с<input v-model="form.quiet_start" type="time" required></label><label>До<input v-model="form.quiet_end" type="time" required></label></div></fieldset>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="saved" class="success-note" role="status">Настройки сохранены.</p>
      <button type="submit" :disabled="busy">{{ busy ? 'Сохраняем…' : onboarding ? 'Начать' : 'Сохранить настройки' }}</button>
    </form>
    <section v-if="!onboarding" class="form-card">
      <h2>Активные сессии</h2>
      <div v-for="session in sessions" :key="session.id" class="list-row">
        <div><strong>{{ session.id === auth.account?.session_id ? 'Эта сессия' : 'Другая сессия' }}</strong><p class="field-note">Вход {{ new Date(session.created_at).toLocaleString('ru-RU') }}</p></div>
        <button class="secondary-button" type="button" @click="revoke(session.id)">Завершить</button>
      </div>
      <button v-if="sessions.length" type="button" class="secondary-button" @click="logoutAll">Выйти на всех устройствах</button>
    </section>
  </section>
</template>
