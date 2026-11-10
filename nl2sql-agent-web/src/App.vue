<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import type { IconName } from '@/components/app-icons'

const route = useRoute()
const sidebarOpen = ref(false)

const navItems: { name: string; label: string; caption: string; icon: IconName }[] = [
  { name: 'retrieval', label: '检索问答', caption: 'Ask', icon: 'ask' },
  { name: 'database', label: '业务库', caption: 'Database', icon: 'database' },
  { name: 'table', label: '表与字段', caption: 'Table', icon: 'table' },
  { name: 'relation', label: '关联关系', caption: 'Relation', icon: 'relation' },
  { name: 'metric', label: '指标', caption: 'Metric', icon: 'metric' },
  { name: 'model', label: '模型', caption: 'Model', icon: 'model' },
  // 数据集页面暂时隐藏：恢复时取消这行注释，并把 router 里对应路由一并放开
  // { name: 'dataset', label: '数据集', caption: 'Dataset', icon: 'dataset' },
]

const currentTitle = computed(() => route.meta.title ?? '元数据管理')

watch(
  () => route.fullPath,
  () => {
    sidebarOpen.value = false
  },
)
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ 'sidebar--open': sidebarOpen }">
      <div class="brand">
        <span class="brand__mark">T2</span>
        <div>
          <p class="brand__name">Text2SQL</p>
          <p class="brand__caption">元数据管理</p>
        </div>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.name"
          class="nav__item"
          :class="{ 'nav__item--active': route.name === item.name }"
          :to="{ name: item.name }"
        >
          <AppIcon :name="item.icon" :size="16" />
          <span>{{ item.label }}</span>
          <span class="nav__caption">{{ item.caption }}</span>
        </RouterLink>
      </nav>

      <p class="sidebar__foot muted-12">接口前缀 /collection · /evaluation · /retrieval · /model</p>
    </aside>

    <div v-if="sidebarOpen" class="sidebar-mask" @click="sidebarOpen = false" />

    <div class="main">
      <header class="topbar">
        <button class="topbar__menu" type="button" aria-label="切换导航" @click="sidebarOpen = !sidebarOpen">
          <AppIcon name="menu" :size="18" />
        </button>
        <div class="crumbs">
          <span class="muted-12">元数据管理</span>
          <span class="crumbs__sep">/</span>
          <span class="crumbs__current">{{ currentTitle }}</span>
        </div>
      </header>

      <main class="content">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  min-height: 100vh;
}

.sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex: none;
  flex-direction: column;
  gap: 24px;
  width: var(--sidebar-width);
  height: 100vh;
  padding: 20px 12px 16px;
  background: var(--surface);
  border-right: 1px solid var(--hairline);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 10px;
}

.brand__mark {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-lg);
  background: var(--ink);
  color: #ffffff;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.brand__name {
  margin: 0;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
}

.brand__caption {
  margin: 1px 0 0;
  font-size: 11px;
  color: var(--muted);
}

.nav {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}

.nav__item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 10px;
  border-radius: var(--radius-lg);
  color: var(--text);
  transition:
    background-color 140ms ease,
    color 140ms ease;
}

.nav__item:hover {
  background: var(--hover);
}

.nav__item--active {
  background: var(--tint-lavender);
  color: var(--accent);
  font-weight: 500;
}

.nav__caption {
  margin-left: auto;
  font-size: 11px;
  letter-spacing: 0.02em;
  color: var(--muted);
}

.nav__item--active .nav__caption {
  color: var(--accent);
  opacity: 0.72;
}

.sidebar__foot {
  margin: 0;
  padding: 0 10px;
}

.main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  flex: none;
  align-items: center;
  gap: 12px;
  height: var(--topbar-height);
  padding: 0 24px;
  background: var(--canvas);
  border-bottom: 1px solid var(--hairline);
}

.topbar__menu {
  display: none;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  padding: 0;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--canvas);
  color: var(--text);
  cursor: pointer;
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
}

.crumbs__sep {
  color: var(--hairline-strong);
}

.crumbs__current {
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
}

.content {
  flex: 1;
  min-width: 0;
}

.sidebar-mask {
  position: fixed;
  inset: 0;
  z-index: 19;
  background: rgba(15, 15, 15, 0.28);
}

@media (max-width: 900px) {
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 20;
    transform: translateX(-100%);
    transition: transform 220ms ease;
  }

  .sidebar--open {
    transform: none;
  }

  .topbar__menu {
    display: inline-flex;
  }
}
</style>
