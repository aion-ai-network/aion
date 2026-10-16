import { createRouter, createWebHistory } from 'vue-router'
import TodayView from '../features/today/TodayView.vue'
import StatusView from '../features/system/StatusView.vue'
import AuthView from '../features/identity/AuthView.vue'
import ProfileView from '../features/identity/ProfileView.vue'
import AccessView from '../features/identity/AccessView.vue'
import KnowledgeView from '../features/knowledge/KnowledgeView.vue'
import ChatView from '../features/assistant/ChatView.vue'
import MemoryView from '../features/assistant/MemoryView.vue'
import NotificationsView from '../features/notifications/NotificationsView.vue'
import { useAuthStore } from '../features/identity/store'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'today', component: TodayView },
    { path: '/status', name: 'status', component: StatusView },
    { path: '/login', component: AuthView },
    { path: '/join', component: AuthView },
    { path: '/reset', component: AuthView },
    { path: '/onboarding', component: ProfileView },
    { path: '/settings', component: ProfileView },
    { path: '/access', component: AccessView },
    { path: '/knowledge/:id?', component: KnowledgeView },
    { path: '/chat', component: ChatView },
    { path: '/memory', component: MemoryView },
    { path: '/notifications', component: NotificationsView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async to => {
  const auth = useAuthStore()
  if (!auth.initialized) await auth.restore()
  if (['/login', '/join', '/reset', '/status'].includes(to.path)) {
    return to.path === '/login' && auth.account ? '/' : true
  }
  if (!auth.account) return '/login'
  if (!auth.account.profile.onboarding_completed && to.path !== '/onboarding') return '/onboarding'
  if (to.path === '/access' && auth.account.user.role !== 'admin') return '/'
  return true
})
