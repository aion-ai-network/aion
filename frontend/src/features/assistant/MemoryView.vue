<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../../shared/api'
import { message } from '../../shared/domain'
interface Memory {id:string;content:string;source_type:string;source_message_id:string|null;created_at:string;version:number}
const rows=ref<Memory[]>([]),content=ref(''),error=ref(''),busy=ref(false)
async function load(){rows.value=await api<Memory[]>('/api/v1/memory')}
async function run(action:()=>Promise<unknown>){if(busy.value)return;busy.value=true;error.value='';try{await action();await load()}catch(e){error.value=message(e)}finally{busy.value=false}}
onMounted(()=>run(load))
</script>
<template><section class="page"><p class="eyebrow">ПАМЯТЬ</p><h1>Важное о вас.</h1><p class="lead">Записи можно исправить или удалить в любой момент.</p><p v-if="error" class="notice" role="alert">{{error}}</p>
  <form class="form-card form-stack" @submit.prevent="run(async()=>{await api('/api/v1/memory',{method:'POST',body:{content}});content=''})"><label>Что сохранить<textarea v-model="content" rows="3" maxlength="2000" required></textarea></label><button :disabled="busy">Сохранить в память</button></form>
  <p v-if="!rows.length" class="quiet-note">Память пока пуста.</p><form v-for="m in rows" :key="m.id" class="form-card form-stack" @submit.prevent="run(()=>api('/api/v1/memory/'+m.id,{method:'PUT',body:{content:m.content,version:m.version}}))"><p class="field-note">{{m.source_type==='conversation'?'Из разговора':'Добавлено вручную'}} · {{new Date(m.created_at).toLocaleDateString('ru')}} · Версия {{m.version}}</p><label>Запись<textarea v-model="m.content" rows="3" maxlength="2000" required></textarea></label><div class="toolbar"><button :disabled="busy">Сохранить правку</button><button type="button" class="secondary-button" :disabled="busy" @click="run(()=>api('/api/v1/memory/'+m.id+'?version='+m.version,{method:'DELETE'}))">Забыть</button></div></form>
</section></template>
