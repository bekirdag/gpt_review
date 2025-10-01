<template>
  <article class="card">
    <h4 class="title">{{ title }}</h4>
    <div class="value">{{ value }}</div>
  </article>
</template>
<script setup lang="ts">
defineProps<{ title: string; value: number | string }>()
</script>
<style scoped>
.card { border: 1px solid var(--border); border-radius: var(--radius-3); padding: var(--space-4); background: var(--surface-1); }
.title { margin: 0 0 var(--space-2); font-weight: 600; color: var(--muted); }
.value { font-size: 1.75rem; font-weight: 700; }
</style>
