<template>
  <section>
    <h1>Settings</h1>
    <form class="stack" @submit.prevent="onSave">
      <label>Site name <input v-model="siteName" /></label>
      <label>Primary color <input v-model="primary" type="color" /></label>
      <button type="submit">Save</button>
    </form>
  </section>
</template>
<script setup lang="ts">
import { ref } from 'vue'
const siteName = ref('Admin')
const primary = ref('#7c3aed')
const onSave = () => console.log({ siteName: siteName.value, primary: primary.value })
</script>
<style scoped>
.stack > * { display:block; margin-bottom: var(--space-3); }
input { padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-2); background: var(--surface-2); color: var(--text); }
</style>
