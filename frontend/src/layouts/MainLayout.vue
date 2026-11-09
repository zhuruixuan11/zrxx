<template>
  <el-container class="main-container">
    <!-- 侧边栏 -->
    <el-aside width="220px" class="aside">
      <div class="logo">
        <el-icon :size="28" color="#c8a45c"><Coin /></el-icon>
        <span>衡策</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        background-color="transparent"
        text-color="#c9d6e4"
        active-text-color="#dfc38c"
      >
        <el-menu-item index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <template #title>仪表盘</template>
        </el-menu-item>
        <el-menu-item index="/market">
          <el-icon><LineChart /></el-icon>
          <template #title>A 股行情</template>
        </el-menu-item>
        <el-menu-item index="/market-overview">
          <el-icon><Grid /></el-icon>
          <template #title>A 股全景</template>
        </el-menu-item>
        <el-menu-item index="/news">
          <el-icon><Reading /></el-icon>
          <template #title>资讯中心</template>
        </el-menu-item>
        <el-menu-item index="/agent">
          <el-icon><ChatDotRound /></el-icon>
          <template #title>衡策智算</template>
        </el-menu-item>
        <el-menu-item index="/datasets">
          <el-icon><Collection /></el-icon>
          <template #title>数据集</template>
        </el-menu-item>
        <el-menu-item index="/backtest">
          <el-icon><DataAnalysis /></el-icon>
          <template #title>策略回测</template>
        </el-menu-item>
        <el-menu-item index="/benchmark">
          <el-icon><Aim /></el-icon>
          <template #title>基准跟随</template>
        </el-menu-item>
        <el-menu-item index="/alerts">
          <el-icon><Bell /></el-icon>
          <template #title>到价预警</template>
        </el-menu-item>
        <el-menu-item index="/watch">
          <el-icon><View /></el-icon>
          <template #title>持仓跟随</template>
        </el-menu-item>
        <el-menu-item index="/profile">
          <el-icon><User /></el-icon>
          <template #title>投资画像</template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <!-- 主区域 -->
    <el-container>
      <el-header class="header" height="56px">
        <div class="header-left">
          <h3>{{ $route.meta.title || '衡策' }}</h3>
        </div>
        <div class="header-right">
          <el-dropdown @command="handleCommand">
            <span class="user-info">
              <el-avatar :size="32" class="user-avatar">
                {{ userStore.user?.username?.[0]?.toUpperCase() || 'U' }}
              </el-avatar>
              <span class="username">{{ userStore.user?.username }}</span>
              <el-icon><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

function handleCommand(cmd: string) {
  if (cmd === 'logout') {
    userStore.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.aside {
  background: linear-gradient(180deg, #071a2c 0%, #0d2b45 55%, #1f4e79 100%);
  overflow-y: auto;
  border-right: 1px solid rgba(200, 164, 92, 0.15);
}
.logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #fff;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 3px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.aside :deep(.el-menu) {
  border-right: none;
  background: transparent;
}
.aside :deep(.el-menu-item) {
  transition: background 0.2s, color 0.2s;
}
.aside :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.07);
}
.aside :deep(.el-menu-item.is-active) {
  background: rgba(200, 164, 92, 0.16);
  box-shadow: inset 3px 0 0 #c8a45c;
  font-weight: 600;
}
.header {
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #e4e9f0;
  box-shadow: 0 1px 4px rgba(13, 35, 56, 0.05);
  padding: 0 20px;
}
.header h3 {
  margin: 0;
  font-size: 16px;
  color: #1f4e79;
}
.user-avatar {
  background: linear-gradient(135deg, #1f4e79 0%, #2a5f8f 100%);
  color: #e6d3ab;
  font-weight: 600;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: background 0.2s;
}
.user-info:hover {
  background: #f2f6fa;
}
.username {
  font-size: 14px;
  color: #20303f;
}
.main {
  padding: 20px;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
