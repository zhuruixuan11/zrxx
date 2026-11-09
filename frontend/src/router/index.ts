/** Vue Router 配置 + 路由守卫 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/Login.vue') },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '仪表盘' } },
      { path: 'market', name: 'market', component: () => import('@/views/Market.vue'), meta: { title: 'A 股行情' } },
      { path: 'market-overview', name: 'market-overview', component: () => import('@/views/MarketOverview.vue'), meta: { title: 'A 股全景' } },
      { path: 'news', name: 'news', component: () => import('@/views/News.vue'), meta: { title: '资讯中心' } },
      { path: 'agent', name: 'agent', component: () => import('@/views/Agent.vue'), meta: { title: '衡策智算' } },
      { path: 'datasets', name: 'datasets', component: () => import('@/views/Datasets.vue'), meta: { title: '数据集' } },
      { path: 'backtest', name: 'backtest', component: () => import('@/views/Backtest.vue'), meta: { title: '策略回测' } },
      { path: 'benchmark', name: 'benchmark', component: () => import('@/views/Benchmark.vue'), meta: { title: '基准跟随' } },
      { path: 'alerts', name: 'alerts', component: () => import('@/views/Alerts.vue'), meta: { title: '到价预警' } },
      { path: 'watch', name: 'watch', component: () => import('@/views/Watch.vue'), meta: { title: '持仓跟随' } },
      { path: 'profile', name: 'profile', component: () => import('@/views/Profile.vue'), meta: { title: '投资画像' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const store = useUserStore()
  if (to.meta.requiresAuth && !store.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && store.isLoggedIn) {
    return '/dashboard'
  }
})

export default router
