<template>
  <footer class="footer">
    <div class="inner">
      <small>&copy; {{ new Date().getFullYear() }} Admin</small>
    </div>
  </footer>
</template>
<script setup lang="ts"></script>
<style scoped>
.footer { border-top: 1px solid var(--border); background: var(--surface-1); }
.inner { max-width: 1280px; margin: 0 auto; padding: var(--space-4); }
</style>
