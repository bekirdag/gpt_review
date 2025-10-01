import { createRouter, createWebHistory } from 'vue-router'

const Dashboard = () => import('./pages/Dashboard.vue')
const Users = () => import('./pages/Users.vue')
const Content = () => import('./pages/Content.vue')
const Settings = () => import('./pages/Settings.vue')
const NotFound = () => import('./pages/NotFound.vue')

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', name: 'dashboard', component: Dashboard },
    { path: '/users', name: 'users', component: Users },
    { path: '/content', name: 'content', component: Content },
    { path: '/settings', name: 'settings', component: Settings },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFound }
  ],
  scrollBehavior() { return { top: 0 } }
})

export default router
