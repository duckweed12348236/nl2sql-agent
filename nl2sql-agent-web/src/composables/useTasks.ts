import { computed, ref } from 'vue'
import { listTasks } from '@/api/evaluation'
import { errorMessage } from '@/plugins/request'
import type { TaskSnapshot } from '@/types/dataset'

/**
 * 后台任务列表：出题、评测与整库采集都写进同一批任务记录，靠 `GET /evaluation/task`
 * 按用户批量取（新的在前，最多 20 条、记录最多留 3 天）。
 *
 * 状态放在模块级，多个页面共享同一份列表与同一个轮询；
 * 只有在列表里还有 `running` 的任务时才继续查，全部收尾自动停下。
 */

const POLL_INTERVAL = 3000

const tasks = ref<TaskSnapshot[]>([])
const loading = ref(false)
const error = ref('')
/** 展开的任务编号（列表刷新时按编号保留展开状态） */
const openNames = ref<string[]>([])
const panelOpen = ref(false)
/** 每有一次任务从「进行中」收尾就加一：页面 watch 它刷新自己的数据 */
const settledTick = ref(0)

let timer: ReturnType<typeof setTimeout> | null = null
/** 每次重新拉取都自增：旧一轮的延迟回包凭令牌直接丢弃 */
let token = 0
let started = false

const runningCount = computed<number>(
  () => tasks.value.filter(item => item.status === 'running').length,
)

function stop(): void {
  token += 1
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
}

function schedule(): void {
  if (timer !== null) return
  if (!tasks.value.some(item => item.status === 'running')) return

  timer = setTimeout(() => {
    timer = null
    void refresh()
  }, POLL_INTERVAL)
}

async function refresh(): Promise<void> {
  const current = token
  const previous = new Map(tasks.value.map(item => [item.id, item.status]))

  try {
    const list = await listTasks()
    if (current !== token) return
    tasks.value = list
    error.value = ''
  } catch (reason) {
    if (current !== token) return
    error.value = errorMessage(reason, '任务列表加载失败')
  }

  if (current !== token) return
  loading.value = false
  schedule()

  const settled = tasks.value.some(
    item => item.status !== 'running' && previous.get(item.id) === 'running',
  )
  if (settled) settledTick.value += 1
}

/** 首次进入任一使用任务的页面时拉一次 */
function ensureStarted(): void {
  if (started) return
  started = true
  void refresh()
}

/** 打开任务面板：立刻查一次，并重排轮询节奏 */
function openPanel(): void {
  panelOpen.value = true
  stop()
  loading.value = true
  void refresh()
}

export function useTasks() {
  return {
    tasks,
    loading,
    error,
    openNames,
    panelOpen,
    runningCount,
    settledTick,
    ensureStarted,
    refresh,
    openPanel,
  }
}
