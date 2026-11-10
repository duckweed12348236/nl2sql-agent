import { createRouter, createWebHistory } from 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 顶栏与侧栏展示的模块名 */
    title: string
    /** 页面标题下的说明 */
    subtitle?: string
  }
}

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      // 主页就是检索问答。路径不用 /retrieval：它与后端接口前缀同名，会被代理转发到后端
      // （刷新这个地址会拿到 405），故用 / 并把旧的 /ask 留作别名
      path: '/',
      alias: '/ask',
      name: 'retrieval',
      component: () => import('@/pages/RetrievalPage.vue'),
      meta: { title: '检索问答', subtitle: '自然语言问题 → SQL 并执行' },
    },
    {
      path: '/database',
      name: 'database',
      component: () => import('@/pages/DatabasePage.vue'),
      meta: { title: '业务库', subtitle: '已登记的业务数据库' },
    },
    {
      path: '/table',
      name: 'table',
      component: () => import('@/pages/TablePage.vue'),
      meta: { title: '表与字段', subtitle: '表与字段的元数据登记' },
    },
    {
      path: '/relation',
      name: 'relation',
      component: () => import('@/pages/RelationPage.vue'),
      meta: { title: '关联关系', subtitle: '字段间的表间关系' },
    },
    {
      path: '/metric',
      name: 'metric',
      component: () => import('@/pages/MetricPage.vue'),
      meta: { title: '指标', subtitle: '跨库使用的指标定义' },
    },
    {
      // 路径避开 /model：代理按前缀转发，/model 与 /models 都会被打到后端，
      // 刷新页面就会拿回后端的 404，故改用 /llm
      path: '/llm',
      name: 'model',
      component: () => import('@/pages/ModelPage.vue'),
      meta: { title: '模型', subtitle: '对话 / 嵌入 / 重排模型的登记与默认' },
    },
    // 数据集页面暂时隐藏（连同侧栏入口）：恢复时取消这段注释即可，
    // 未注册的 /dataset 会落到下面的兜底路由回主页。
    // {
    //   path: '/dataset',
    //   name: 'dataset',
    //   component: () => import('@/pages/EvaluationPage.vue'),
    //   meta: { title: '数据集', subtitle: '评测数据集的出题与评估' },
    // },
    { path: '/:pathMatch(.*)*', redirect: { name: 'retrieval' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

export default router
