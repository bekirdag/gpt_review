import { createRouter, createWebHistory } from 'vue-router'

const Home = () => import('./pages/Home.vue')
const About = () => import('./pages/About.vue')
const Login = () => import('./pages/Login.vue')
const NotFound = () => import('./pages/NotFound.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: Home },
    { path: '/about', name: 'about', component: About },
    { path: '/login', name: 'login', component: Login },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFound }
  ],
  scrollBehavior() {
    return { top: 0 }
  }
})

export default router
