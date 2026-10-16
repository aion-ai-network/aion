<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../shared/api'
import { message, type Capabilities } from '../../shared/domain'
interface Message {id:string;sequence:number;role:string;content:string;channel:string;state:string;source_type:string}
interface Run {id:string;state:string}
const rows=ref<Message[]>([]), runs=ref<Run[]>([]), capabilities=ref<Capabilities | null>(null)
const content=ref(''),error=ref(''),busy=ref(false),key=ref(crypto.randomUUID())
const processing=computed(()=>runs.value.some(r=>['pending','running'].includes(r.state)))
let events: EventSource | undefined
async function load() {const data=await api<{messages:Message[];runs:Run[]}>('/api/v1/conversation');rows.value=data.messages.sort((a,b)=>a.sequence-b.sequence);runs.value=data.runs}
async function send() {
  if(busy.value)return
  busy.value=true;error.value=''
  try {await api('/api/v1/conversation/messages',{method:'POST',body:{content:content.value,request_key:key.value}});content.value='';key.value=crypto.randomUUID();await load()}
  catch(e){error.value=message(e)}finally{busy.value=false}
}
onMounted(async()=>{try{
  capabilities.value=await api<Capabilities>('/api/v1/capabilities');await load()
  events=new EventSource('/api/v1/conversation/events')
  events.addEventListener('change',()=>{void load().catch(e=>{error.value=message(e)})})
  events.addEventListener('session',()=>{events?.close();error.value='Сессия завершена. Войдите снова.'})
}catch(e){error.value=message(e)}})
onUnmounted(()=>events?.close())
</script>
<template><section class="page"><p class="eyebrow">РАЗГОВОР</p><h1>Разберём по одному.</h1><p class="lead">Общая история веба и Telegram.</p>
  <p v-if="capabilities && !capabilities.llm" class="notice">Модель пока не подключена. План, статьи и память доступны через обычные формы.</p><p v-if="error" class="notice" role="alert">{{ error }}</p>
  <div class="form-card chat-history" aria-live="polite"><p v-if="!rows.length" class="quiet-note">Здесь появится разговор.</p><article v-for="m in rows" :key="m.id" class="chat-message" :class="m.role"><p class="field-note">{{m.role==='user'?'Вы':'AION'}} · {{m.channel==='telegram'?'Telegram':'Веб'}} {{m.source_type==='voice'?'· Голос':''}}</p><div class="prose-text">{{m.content}}</div></article><p v-if="processing" class="quiet-note">Обрабатываем запрос…</p><p v-if="runs.some(r=>r.state==='failed')" class="field-note">Часть запросов не удалось обработать. Можно отправить сообщение ещё раз.</p></div>
  <form class="form-card form-stack" @submit.prevent="send"><label>Сообщение<textarea v-model="content" rows="4" required maxlength="12000" :disabled="!capabilities?.llm" placeholder="Например: добавь задачу и запомни важную деталь"></textarea></label><button :disabled="busy || !capabilities?.llm">Отправить</button></form>
</section></template>
