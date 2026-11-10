<script setup lang="ts">
import { computed } from 'vue'
import { ICON_PATHS, type IconName } from './app-icons'

const props = withDefaults(defineProps<{ name: IconName; size?: number }>(), { size: 16 })

const markup = computed(() => ICON_PATHS[props.name])
</script>

<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="markup"
  />
</template>

<style scoped>
.app-icon {
  display: inline-block;
  flex: none;
  vertical-align: -0.15em;
}
</style>
