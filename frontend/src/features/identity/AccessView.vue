<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../../shared/api'

interface Invite { id: string; email: string; expires_at: string; revoked_at: string | null; used_at: string | null }
interface Member { id: string; email: string; role: string; status: 'active' | 'disabled' }
interface Limits {user_id:string;ai_daily_requests:number;audio_daily_seconds:number;storage_limit_mb:number}
interface Operations {jobs:{kind:string;state:string;count:number}[];usage:{requests:number;cost_usd:number};errors:{kind:string;error_code:string;count:number}[]}
const limits=ref<Limits[]>([]), operations=ref<Operations|null>(null)
const invites = ref<Invite[]>([])
const members = ref<Member[]>([])
const email = ref('')
const link = ref('')
const error = ref('')
const busy = ref(false)
function state(invite: Invite) {
  return invite.used_at ? 'Использовано' : invite.revoked_at ? 'Отозвано' : new Date(invite.expires_at) <= new Date() ? 'Истекло' : 'Действует'
}
async function refresh() {
  const values = await Promise.all([api<Invite[]>('/api/v1/admin/invites'), api<Member[]>('/api/v1/admin/users')])
  invites.value = values[0]
  members.value = values[1]
  limits.value=await api<Limits[]>('/api/v1/admin/limits')
  operations.value=await api<Operations>('/api/v1/admin/operations')
}
async function action(work: () => Promise<void>) {
  busy.value = true; error.value = ''
  try { await work(); await refresh() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : 'Не удалось выполнить действие.' }
  finally { busy.value = false }
}
async function issue() {
  link.value = ''
  await action(async () => {
    const created = await api<{ link: string }>('/api/v1/admin/invites', { method: 'POST', body: { email: email.value, hours: 48 } })
    link.value = created.link; email.value = ''
  })
}
async function revoke(id: string) { await action(async () => { await api(`/api/v1/admin/invites/${id}`, { method: 'DELETE' }) }) }
async function access(member: Member) {
  await action(async () => { await api(`/api/v1/admin/users/${member.id}/access`, { method: 'PUT', body: { status: member.status === 'active' ? 'disabled' : 'active' } }) })
}
onMounted(() => { void action(async () => {}) })
</script>

<template>
  <section class="page access-page">
    <p class="eyebrow">ЗАКРЫТАЯ ГРУППА</p><h1>Доступ и приглашения</h1>
    <p class="lead">У каждого участника своё пространство.</p>
    <p v-if="error" role="alert" class="notice">{{ error }}</p>
    <form class="form-card form-stack" @submit.prevent="issue">
      <label>Email нового участника<input v-model="email" type="email" autocomplete="off" maxlength="254" required></label>
      <button type="submit" :disabled="busy">Создать приглашение</button>
      <template v-if="link"><label>Одноразовая ссылка<textarea :value="link" readonly rows="3" aria-label="Ссылка приглашения"></textarea></label><p class="field-note" role="status">Передайте ссылку участнику. Она действует 48 часов и показывается только сейчас.</p></template>
    </form>
    <section class="form-card"><h2>Приглашения</h2>
      <p v-if="!invites.length" class="field-note">Приглашений пока нет.</p>
      <div v-for="invite in invites" :key="invite.id" class="list-row"><div><strong>{{ invite.email }}</strong><p class="field-note">{{ state(invite) }}</p></div><button v-if="state(invite) === 'Действует'" class="secondary-button" :disabled="busy" @click="revoke(invite.id)">Отозвать</button></div>
    </section>
    <section class="form-card"><h2>Лимиты участников</h2><form v-for="l in limits" :key="l.user_id" class="domain-row form-stack" @submit.prevent="action(async()=>{const {user_id,...body}=l;await api('/api/v1/admin/users/'+user_id+'/limits',{method:'PUT',body})})"><strong>{{members.find(m=>m.id===l.user_id)?.email}}</strong><div class="form-grid"><label>Запросов ИИ в день<input v-model.number="l.ai_daily_requests" type="number" min="0" max="1000" required></label><label>Секунд голоса в день<input v-model.number="l.audio_daily_seconds" type="number" min="0" max="36000" required></label><label>Хранилище, МБ<input v-model.number="l.storage_limit_mb" type="number" min="0" max="100000" required></label></div><button :disabled="busy">Сохранить лимиты</button></form></section>
    <section v-if="operations" class="form-card"><h2>Работа сервиса</h2><p class="field-note">Сегодня: {{operations.usage.requests}} запросов, учтено ${{operations.usage.cost_usd}}. Содержимое разговоров и статей здесь не отображается.</p><div v-for="j in operations.jobs" :key="j.kind+j.state" class="list-row"><span>{{j.kind}} · {{j.state}}</span><strong>{{j.count}}</strong></div><p v-for="e in operations.errors" :key="e.kind+e.error_code" class="field-note">{{e.kind}} · {{e.error_code}}: {{e.count}}</p></section>
    <section class="form-card"><h2>Участники</h2>
      <div v-for="member in members" :key="member.id" class="list-row"><div><strong>{{ member.email }}</strong><p class="field-note">{{ member.role === 'admin' ? 'Администратор' : member.status === 'active' ? 'Доступ открыт' : 'Доступ закрыт' }}</p></div><button v-if="member.role !== 'admin'" class="secondary-button" :disabled="busy" @click="access(member)">{{ member.status === 'active' ? 'Закрыть доступ' : 'Открыть доступ' }}</button></div>
    </section>
  </section>
</template>
