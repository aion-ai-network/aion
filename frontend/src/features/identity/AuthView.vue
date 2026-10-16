<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../../shared/api'
import { useAuthStore } from './store'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const mode = computed(() => route.path === '/join' ? 'join' : route.path === '/reset' ? 'reset' : 'login')
const token = computed(() => route.hash.slice(1))
const email = ref('')
const name = ref('')
const password = ref('')
const confirmation = ref('')
const busy = ref(false)
const error = ref('')
const resetDone = ref(false)
const title = computed(() => mode.value === 'join' ? 'Добро пожаловать в AION' : mode.value === 'reset' ? 'Новый пароль' : 'С возвращением')

async function submit() {
  error.value = ''
  if (mode.value !== 'login' && password.value !== confirmation.value) {
    error.value = 'Пароли не совпадают.'
    return
  }
  busy.value = true
  try {
    if (mode.value === 'reset') {
      await api('/api/v1/auth/reset-password', { method: 'POST', body: { token: token.value, password: password.value } })
      auth.clear()
      password.value = confirmation.value = ''
      resetDone.value = true
      await router.replace('/reset')
    } else {
      if (mode.value === 'join') await auth.register(token.value, password.value, name.value)
      else await auth.login(email.value, password.value)
      password.value = confirmation.value = ''
      await router.replace(auth.account?.profile.onboarding_completed ? '/' : '/onboarding')
    }
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось продолжить.' }
  finally { busy.value = false }
}
</script>

<template>
  <section class="page auth-page">
    <p class="eyebrow">ВАШЕ ПРОСТРАНСТВО</p>
    <h1>{{ title }}</h1>
    <p class="lead">{{ mode === 'join' ? 'Создайте аккаунт, чтобы начать в своём темпе.' : mode === 'reset' ? 'После смены пароля потребуется войти заново.' : 'Войдите, чтобы продолжить со своими делами.' }}</p>
    <div v-if="resetDone" class="form-card">
      <p role="status">Пароль изменён. Все прежние сессии завершены.</p>
      <RouterLink to="/login" class="text-link">Перейти ко входу</RouterLink>
    </div>
    <div v-else-if="mode !== 'login' && !token" class="notice" role="alert">
      Откройте полную ссылку {{ mode === 'join' ? 'приглашения' : 'восстановления' }}, которую выдал администратор.
    </div>
    <form v-else class="form-card form-stack" @submit.prevent="submit">
      <label v-if="mode === 'login'">Email<input v-model="email" type="email" autocomplete="username" required maxlength="254"></label>
      <label v-if="mode === 'join'">Как к вам обращаться<input v-model="name" autocomplete="nickname" required maxlength="80"></label>
      <label>Пароль<input v-model="password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" :minlength="mode === 'login' ? 1 : 12" maxlength="256" required></label>
      <template v-if="mode !== 'login'">
        <p class="field-note">Не менее 12 символов. Можно использовать длинную фразу.</p>
        <label>Повторите пароль<input v-model="confirmation" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
      </template>
      <p v-if="error || auth.error" class="form-error" role="alert">{{ error || auth.error }}</p>
      <button type="submit" :disabled="busy">{{ busy ? 'Подождите…' : mode === 'join' ? 'Создать аккаунт' : mode === 'reset' ? 'Сохранить пароль' : 'Войти' }}</button>
      <p v-if="mode === 'login'" class="field-note">Доступ по приглашению. Если забыли пароль, попросите администратора выдать ссылку восстановления.</p>
      <RouterLink v-else to="/login" class="text-link">Уже есть аккаунт? Войти</RouterLink>
    </form>
  </section>
</template>
