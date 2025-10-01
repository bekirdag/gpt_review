<template>
  <section>
    <h1>Dashboard</h1>
    <div class="grid">
      <StatCard title="Users" :value="1284" />
      <StatCard title="Active Sessions" :value="93" />
      <StatCard title="Errors (24h)" :value="2" />
    </div>
  </section>
</template>
<script setup lang="ts">
import StatCard from '../components/StatCard.vue'
</script>
<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: var(--space-4); }
</style>
