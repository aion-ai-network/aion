<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../../shared/api'
import { displayTime, localTime, message, type Activity, type Plan, type Task } from '../../shared/domain'
import { useAuthStore } from '../identity/store'
const auth = useAuthStore()
const plan = ref<Plan | null>(null), error = ref(''), busy = ref(false), loading = ref(true)
const editor = ref<'task' | 'activity' | ''>('')
const projectTitle = ref(''), showClosed = ref(false), resultText = ref('')
const zone = computed(() => auth.account?.profile.timezone ?? 'UTC')
const blankTask = () => ({ id: '', title: '', description: '', first_step: '', project_id: '', due_at: '', priority: 2, estimated_minutes: 25, status: 'open' as Task['status'], version: 1 })
const blankActivity = () => ({ id: '', title: '', notes: '', task_id: '', starts_at: '', ends_at: '', fixed: false, keep_on_pause: false, notification_overrides: {} as Record<string, boolean | null>, version: 1 })
const task = ref(blankTask()), activity = ref(blankActivity())
const tasks = computed(() => plan.value?.tasks.filter(t => showClosed.value || t.status === 'open') ?? [])
const activities = computed(() => [...(plan.value?.activities ?? [])].filter(a => showClosed.value || a.outcome === 'unknown').sort((a,b) => a.starts_at.localeCompare(b.starts_at)))
async function load() { plan.value = await api<Plan>('/api/v1/plan') }
async function run(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await action(); await load() } catch (e) { error.value = message(e) } finally { busy.value = false }
}
function editTask(t?: Task) { task.value = t ? { ...t, project_id: t.project_id ?? '', due_at: localTime(t.due_at, zone.value) } : blankTask(); editor.value = 'task' }
function editActivity(a?: Activity) { activity.value = a ? { ...a, task_id: a.task_id ?? '', starts_at: localTime(a.starts_at, zone.value), ends_at: localTime(a.ends_at, zone.value) } : blankActivity(); editor.value = 'activity' }
async function saveTask() { await run(async () => {
  const { id, version, ...fields } = task.value
  await api(`/api/v1/tasks${id ? '/' + id : ''}`, { method: id ? 'PUT' : 'POST', body: { ...fields, project_id: fields.project_id || null, due_at: fields.due_at || null, ...(id ? { version } : {}) } }); editor.value = ''
}) }
async function saveActivity() { await run(async () => {
  const { id, version, title, notes, task_id, starts_at, ends_at, fixed, keep_on_pause, notification_overrides } = activity.value
  await api(`/api/v1/activities${id ? '/' + id : ''}`, { method: id ? 'PUT' : 'POST', body: { title, notes, task_id: task_id || null, starts_at, ends_at, fixed, keep_on_pause, notification_overrides, ...(id ? { version } : {}) } }); editor.value = ''
}) }
async function outcome(a: Activity, value: string) { await run(async () => { await api(`/api/v1/activities/${a.id}/result`, { method: 'POST', body: { version: a.version, outcome: value, content: resultText.value } }); resultText.value = '' }) }
async function saveResult(a: Activity) { await run(() => api('/api/v1/articles', { method: 'POST', body: { title: a.title, body: a.result_text, source_text: a.result_text, activity_id: a.id } })) }
onMounted(async () => { try { await load() } catch (e) { error.value = message(e) } finally { loading.value = false } })
</script>
<template>
  <section class="page wide-page">
    <p class="eyebrow">ВАШЕ ПРОСТРАНСТВО</p><h1>Один шаг за раз.</h1><p class="lead">План и время — {{ zone }}.</p>
    <p v-if="error" class="notice" role="alert">{{ error }} <button @click="run(load)">Обновить</button></p><p v-if="loading" class="quiet-note">Загружаем план…</p>
    <template v-if="plan">
      <div class="toolbar" aria-label="Режим нагрузки"><button v-for="m in [{key:'normal',label:'Обычный'}, {key:'light',label:'Лёгкий'}, {key:'paused',label:'Пауза'}]" :key="m.key" :class="{'secondary-button': plan.focus.mode !== m.key}" :disabled="busy" @click="run(() => api('/api/v1/mode', {method:'PUT',body:{mode:m.key}}))">{{ m.label }}</button></div>
      <div class="form-card focus-card"><h2>Следующее действие</h2><p class="field-note">{{ plan.focus.explanation }}</p>
        <div v-for="t in plan.focus.priorities" :key="t.id" class="focus-item"><strong>{{ t.first_step }}</strong><p>{{ t.title }}</p><p class="field-note">{{ t.reason }}</p></div>
        <p v-if="!plan.focus.priorities.length" class="quiet-note">{{ plan.focus.mode === 'paused' ? 'Можно вернуться, когда будете готовы.' : 'Подходящих задач на доступное время пока нет.' }}</p>
        <p v-if="plan.focus.activities[0]" class="field-note">Ближайшее: {{ plan.focus.activities[0].title }} — {{ displayTime(plan.focus.activities[0].starts_at, zone) }}</p>
      </div>
      <div class="toolbar"><button @click="editTask()">Добавить задачу</button><button class="secondary-button" @click="editActivity()">Запланировать время</button></div>
      <form v-if="editor === 'task'" class="form-card form-stack" @submit.prevent="saveTask"><h2>{{ task.id ? 'Изменить задачу' : 'Новая задача' }}</h2>
        <label>Желаемый результат<input v-model="task.title" required maxlength="240"></label><label>Первый маленький шаг<input v-model="task.first_step" required maxlength="240"></label><label>Описание<textarea v-model="task.description" rows="3" maxlength="20000"></textarea></label>
        <div class="form-grid"><label>Срок ({{ zone }})<input v-model="task.due_at" type="datetime-local"></label><label>Проект<select v-model="task.project_id"><option value="">Без проекта</option><option v-for="p in plan.projects" :key="p.id" :value="p.id">{{ p.title }}</option></select></label><label>Приоритет<select v-model.number="task.priority"><option :value="1">1 — высокий</option><option :value="2">2 — обычный</option><option :value="3">3 — низкий</option></select></label><label>Примерно минут<input v-model.number="task.estimated_minutes" type="number" min="1" max="1440" required></label><label>Состояние<select v-model="task.status"><option value="open">Открыта</option><option value="done">Готова</option><option value="cancelled">Отменена</option></select></label></div>
        <div class="toolbar"><button :disabled="busy">Сохранить задачу</button><button type="button" class="secondary-button" @click="editor=''">Закрыть</button></div>
      </form>
      <form v-if="editor === 'activity'" class="form-card form-stack" @submit.prevent="saveActivity"><h2>{{ activity.id ? 'Изменить или перенести' : 'Запланировать активность' }}</h2><label>Что запланировано<input v-model="activity.title" required maxlength="240"></label>
        <div class="form-grid"><label>Начало ({{ zone }})<input v-model="activity.starts_at" type="datetime-local" required></label><label>Конец<input v-model="activity.ends_at" type="datetime-local" required></label></div><label>Связанная задача<select v-model="activity.task_id"><option value="">Без задачи</option><option v-for="t in plan.tasks" :key="t.id" :value="t.id">{{ t.title }}</option></select></label><label>Заметки<textarea v-model="activity.notes" rows="3" maxlength="20000"></textarea></label><label class="checkbox-label"><input v-model="activity.fixed" type="checkbox">Фиксированное обязательство</label><label class="checkbox-label"><input v-model="activity.keep_on_pause" type="checkbox">Напоминать даже на паузе</label>
        <div class="form-grid"><label v-for="r in [{key:'reminders',label:'Напоминание'},{key:'progress',label:'Прогресс'},{key:'results',label:'Результат'}]" :key="r.key">{{ r.label }}<select v-model="activity.notification_overrides[r.key]"><option :value="undefined">Общее правило</option><option :value="true">Включено</option><option :value="false">Выключено</option></select></label></div>
        <div class="toolbar"><button :disabled="busy">Сохранить время</button><button type="button" class="secondary-button" @click="editor=''">Закрыть</button></div>
      </form>
      <div class="form-card"><h2>План</h2><label class="checkbox-label"><input v-model="showClosed" type="checkbox">Показать завершённые</label><p v-if="!activities.length" class="quiet-note">Временных активностей пока нет.</p>
        <article v-for="a in activities" :key="a.id" class="domain-row"><div><strong>{{ a.title }}</strong><p class="field-note">{{ displayTime(a.starts_at,zone) }} — {{ displayTime(a.ends_at,zone) }} · {{ a.fixed ? 'Фиксировано' : 'Гибко' }} · {{ {unknown:'Исход пока неизвестен',done:'Сделано',partial:'Частично',cancelled:'Отменено'}[a.outcome] }}</p><p v-if="a.result_text" class="prose-text">{{ a.result_text }}</p></div><div v-if="a.outcome==='unknown'" class="toolbar"><button class="secondary-button" @click="editActivity(a)">Изменить / перенести</button><button :disabled="busy" @click="outcome(a,'done')">Сделал</button><button class="secondary-button" :disabled="busy" @click="outcome(a,'partial')">Частично</button><button class="secondary-button" :disabled="busy" @click="outcome(a,'cancelled')">Отменил</button></div><button v-else-if="a.result_text" class="secondary-button" :disabled="busy" @click="saveResult(a)">Сохранить результат в статьи</button></article>
        <label class="field-note">Комментарий к следующему результату<textarea v-model="resultText" maxlength="20000" rows="2" placeholder="Что получилось? Необязательно."></textarea></label>
      </div>
      <div class="form-card"><h2>Задачи</h2><p v-if="!tasks.length" class="quiet-note">Добавьте результат и первый шаг.</p><div v-for="t in tasks" :key="t.id" class="list-row"><div><strong>{{ t.title }}</strong><p>{{ t.first_step }}</p><p class="field-note">Приоритет {{ t.priority }} · {{ t.estimated_minutes }} мин. <span v-if="t.due_at">· До {{ displayTime(t.due_at,zone) }}</span></p></div><button class="secondary-button" @click="editTask(t)">Изменить</button></div></div>
      <form class="form-card form-stack" @submit.prevent="run(async () => { await api('/api/v1/projects',{method:'POST',body:{title:projectTitle}}); projectTitle='' })"><h2>Проекты</h2><p class="field-note">{{ plan.projects.map(p=>p.title).join(' · ') || 'Проектов пока нет.' }}</p><label>Новый проект<input v-model="projectTitle" maxlength="160" required></label><button :disabled="busy">Добавить проект</button></form>
    </template>
  </section>
</template>
