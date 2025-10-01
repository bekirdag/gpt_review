<template>
  <article class="card">
    <header v-if="$slots.title" class="card__header">
      <h3 class="card__title"><slot name="title" /></h3>
    </header>
    <section class="card__body">
      <slot />
    </section>
  </article>
</template>
<script setup lang="ts"></script>
<style scoped>
.card { border: 1px solid var(--border); border-radius: var(--radius-3); padding: var(--space-4); background: var(--surface-1); }
.card__header { margin-bottom: var(--space-3); }
.card__title { margin: 0; font-size: 1.1rem; }
</style>
