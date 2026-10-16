<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, uploadFile } from '../../shared/api'
import { message, type Plan } from '../../shared/domain'
interface Article {
  id: string; title: string; body: string; source_text: string; tags: string[]; version: number
  project_id: string | null; task_id: string | null; activity_id: string | null
  source_type: string; source_id: string | null; attachments?: Attachment[]
}
interface Attachment { id: string; filename: string; state: string; size_bytes: number }
interface Revision { version: number; title: string; body: string; source_text: string; created_at: string }
interface Export { id: string; state: string; size_bytes: number | null; expires_at: string }
const md = new MarkdownIt({ html: false, linkify: false, breaks: true })
const route = useRoute(), router = useRouter()
const rows = ref<Article[]>([]), current = ref<Article | null>(null), revisions = ref<Revision[]>([])
const exports = ref<Export[]>([]), plan = ref<Plan | null>(null)
const query = ref(''), tag = ref(''), project = ref(''), since = ref(''), until = ref('')
const error = ref(''), busy = ref(false), editing = ref(false), tags = ref('')
const rendered = computed(() => md.render(current.value?.body ?? ''))
const blank = (): Article => ({id:'',title:'',body:'',source_text:'',tags:[],version:1,project_id:null,task_id:null,activity_id:null,source_type:'manual',source_id:null})
async function catalog() {
  const params = new URLSearchParams({q:query.value})
  if (tag.value) params.set('tag',tag.value)
  if (project.value) params.set('project',project.value)
  if (since.value) params.set('since',since.value+'T00:00:00Z')
  if (until.value) params.set('until',until.value+'T23:59:59Z')
  rows.value = await api<Article[]>('/api/v1/articles?'+params)
  exports.value = await api<Export[]>('/api/v1/exports')
}
async function open() {
  error.value = ''; revisions.value = []; editing.value = false
  try { current.value = route.params.id ? await api<Article>('/api/v1/articles/'+route.params.id) : null; tags.value = current.value?.tags.join(', ') ?? '' }
  catch(e) { error.value = message(e) }
}
async function run(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value=true; error.value=''
  try { await action() } catch(e) { error.value=message(e) } finally { busy.value=false }
}
function create() { current.value=blank(); tags.value=''; editing.value=true; revisions.value=[] }
async function save() {
  const a = current.value
  if (!a) return
  await run(async () => {
    const body = {title:a.title,body:a.body,source_text:a.source_text,tags:tags.value.split(',').map(t=>t.trim()).filter(Boolean),project_id:a.project_id||null,task_id:a.task_id||null,activity_id:a.activity_id||null,...(a.id?{version:a.version}:{})}
    current.value = await api<Article>('/api/v1/articles'+(a.id?'/'+a.id:''),{method:a.id?'PUT':'POST',body})
    editing.value=false; await catalog(); await router.replace('/knowledge/'+current.value.id)
  })
}
async function attach(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file || !current.value?.id) return
  await run(async () => {
    const reserved=await api<Attachment>(`/api/v1/articles/${current.value!.id}/attachments`,{method:'POST',body:{filename:file.name,media_type:file.type||'application/octet-stream',size_bytes:file.size}})
    await uploadFile(`/api/v1/attachments/${reserved.id}/content`,file); await open()
  })
}
watch(()=>route.params.id,open)
onMounted(async () => { await run(async()=>{await catalog(); plan.value=await api<Plan>('/api/v1/plan'); await open()}) })
</script>
<template>
  <section class="page wide-page"><p class="eyebrow">БАЗА ЗНАНИЙ</p><h1>Сохранённое рядом.</h1><p class="lead">Статьи, исходные материалы и связанные дела.</p><p v-if="error" class="notice" role="alert">{{ error }}</p>
    <form class="form-card form-stack" @submit.prevent="run(catalog)"><label>Поиск по тексту<input v-model="query" maxlength="500" placeholder="Слово или фраза"></label><div class="form-grid"><label>Метка<input v-model="tag"></label><label>Проект<select v-model="project"><option value="">Все</option><option v-for="p in plan?.projects" :value="p.id" :key="p.id">{{ p.title }}</option></select></label><label>Создано с<input v-model="since" type="date"></label><label>По дату<input v-model="until" type="date"></label></div><div class="toolbar"><button :disabled="busy">Найти</button><button type="button" class="secondary-button" @click="create">Новая статья</button></div></form>
    <div v-if="current" class="form-card">
      <form v-if="editing" class="form-stack" @submit.prevent="save"><label>Заголовок<input v-model="current.title" required maxlength="240"></label><label>Markdown<textarea v-model="current.body" rows="14" maxlength="200000"></textarea></label><label>Исходный материал<textarea v-model="current.source_text" rows="5" maxlength="200000"></textarea></label><label>Метки через запятую<input v-model="tags"></label>
        <div class="form-grid"><label>Проект<select v-model="current.project_id"><option :value="null">Без проекта</option><option v-for="p in plan?.projects" :key="p.id" :value="p.id">{{ p.title }}</option></select></label><label>Задача<select v-model="current.task_id"><option :value="null">Без задачи</option><option v-for="t in plan?.tasks" :key="t.id" :value="t.id">{{ t.title }}</option></select></label><label>Активность<select v-model="current.activity_id"><option :value="null">Без активности</option><option v-for="a in plan?.activities" :key="a.id" :value="a.id">{{ a.title }}</option></select></label></div>
        <div class="toolbar"><button :disabled="busy">Сохранить статью</button><button type="button" class="secondary-button" @click="open">Отменить правку</button></div></form>
      <template v-else><h2>{{ current.title }}</h2><p class="field-note">{{ current.tags.join(' · ') }} · Версия {{ current.version }} · Источник: {{ current.source_type }}</p><div class="markdown" v-html="rendered"></div><details v-if="current.source_text"><summary>Исходный материал</summary><pre class="prose-text">{{ current.source_text }}</pre></details><p v-if="current.activity_id" class="field-note">Связано с активностью в <RouterLink to="/">плане</RouterLink>.</p>
        <div class="toolbar"><button @click="editing=true">Редактировать</button><button class="secondary-button" @click="run(async()=>{revisions=await api<Revision[]>('/api/v1/articles/'+current!.id+'/revisions')})">Версии</button><button class="secondary-button" :disabled="busy" @click="run(async()=>{await api('/api/v1/articles/'+current!.id,{method:'DELETE'});current=null;await router.push('/knowledge');await catalog()})">Удалить статью</button></div>
        <div v-for="r in revisions" :key="r.version" class="domain-row"><details><summary>Версия {{ r.version }} · {{ r.title }}</summary><pre class="prose-text">{{ r.body }}</pre><p class="field-note">Исходник</p><pre class="prose-text">{{ r.source_text }}</pre></details></div><h3>Вложения</h3><p v-for="f in current.attachments" :key="f.id"><a v-if="f.state==='ready'" class="text-link" :href="`/api/v1/attachments/${f.id}/content`">{{ f.filename }} ({{ f.size_bytes }} байт)</a><span v-else>{{ f.filename }} — загрузка не завершена</span></p><label class="field-note">Добавить файл до 10 МБ<input type="file" :disabled="busy" @change="attach"></label>
        <div v-if="current.attachments?.length" class="toolbar"><button v-for="f in current.attachments" :key="f.id" class="secondary-button" :disabled="busy" @click="run(async()=>{await api('/api/v1/attachments/'+f.id,{method:'DELETE'});await open()})">Удалить {{ f.filename }}</button></div>
      </template>
    </div>
    <div class="form-card"><h2>Каталог</h2><p v-if="!rows.length" class="quiet-note">Статей пока нет или поиск не дал результатов.</p><RouterLink v-for="a in rows" :key="a.id" :to="'/knowledge/'+a.id" class="domain-row article-link"><strong>{{ a.title }}</strong><p class="field-note">{{ a.tags.join(' · ') }}</p></RouterLink></div>
    <div class="form-card"><h2>Экспорт</h2><p class="field-note">ZIP: Markdown, исходники, метаданные и вложения. Ссылка действует сутки.</p><div class="toolbar"><button :disabled="busy" @click="run(async()=>{await api('/api/v1/exports',{method:'POST'});await catalog()})">Создать архив</button><button class="secondary-button" @click="run(catalog)">Обновить статус</button></div><p v-for="e in exports" :key="e.id"><a v-if="e.state==='ready' && new Date(e.expires_at)>new Date()" class="text-link" :href="`/api/v1/exports/${e.id}/content`">Скачать архив · {{ e.size_bytes }} байт</a><span v-else class="field-note">{{ e.state==='pending' ? 'Архив готовится…' : 'Архив недоступен' }}</span></p></div>
  </section>
</template>
