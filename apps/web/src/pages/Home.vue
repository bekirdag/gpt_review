<template>
  <section>
    <h1>Welcome</h1>
    <p class="muted">Vue 3 + Vite starter.</p>
    <BaseCard>
      <template #title>Hello 👋</template>
      <p>Edit this page at <code>src/pages/Home.vue</code>.</p>
    </BaseCard>
  </section>
</template>
<script setup lang="ts">
import BaseCard from '../components/BaseCard.vue'
</script>
