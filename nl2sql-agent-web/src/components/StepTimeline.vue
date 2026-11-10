<script setup lang="ts">
import type { Step } from '@/types/dataset'

defineProps<{
  steps: Step[]
  running?: boolean
}>()

/** 任务帧的失败状态有 `failed` 与 `error` 两种写法，渲染上统一按失败处理 */
function stepClass(status: Step['status']): string {
  return status === 'error' ? 'step--failed' : `step--${status}`
}
</script>

<template>
  <ol class="steps">
    <li
      v-for="(step, index) in steps"
      :key="index"
      class="step"
      :class="stepClass(step.status)"
    >
      <span class="step__dot" />
      <div class="step__body">
        <p class="step__message">{{ step.message }}</p>
        <p v-if="step.error" class="step__error">{{ step.error }}</p>
      </div>
    </li>

    <li v-if="running" class="step step--running">
      <span class="step__dot step__dot--wait" />
      <div class="step__body">
        <p class="step__message muted">等待下一帧…</p>
      </div>
    </li>
  </ol>
</template>

<style scoped>
.steps {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.step {
  position: relative;
  display: flex;
  gap: 10px;
  padding-bottom: 14px;
}

.step::before {
  content: '';
  position: absolute;
  top: 14px;
  bottom: 0;
  left: 4px;
  width: 1px;
  background: var(--hairline);
}

.step:last-child {
  padding-bottom: 0;
}

.step:last-child::before {
  display: none;
}

.step__dot {
  z-index: 1;
  flex: none;
  width: 9px;
  height: 9px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--muted);
}

.step--running .step__dot {
  background: var(--accent);
}

.step--success .step__dot {
  background: var(--success);
}

.step--failed .step__dot {
  background: var(--danger);
}

/* 运行中的低频脉冲：只动 opacity 与 transform */
.step__dot--wait {
  animation: step-pulse 1.6s ease-in-out infinite;
}

.step__body {
  min-width: 0;
}

.step__message {
  margin: 0;
  font-size: 13px;
  color: var(--text);
}

.step--failed .step__message {
  color: var(--danger);
}

.step__error {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--danger);
}

@keyframes step-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.4;
    transform: scale(0.75);
  }
}
</style>
