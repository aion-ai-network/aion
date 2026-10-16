import { createRouter, createWebHistory } from 'vue-router'
import TodayView from '../features/today/TodayView.vue'
import StatusView from '../features/system/StatusView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'today', component: TodayView },
    { path: '/status', name: 'status', component: StatusView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
