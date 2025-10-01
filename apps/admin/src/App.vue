<template>
  <div id="admin-app">
    <AdminNav />
    <main class="container">
      <router-view />
    </main>
    <AdminFooter />
  </div>
</template>

<script setup lang="ts">
import AdminNav from './components/AdminNav.vue'
import AdminFooter from './components/AdminFooter.vue'
</script>

<style>
#admin-app { min-height: 100vh; display: flex; flex-direction: column; }
main.container { flex: 1 1 auto; padding: var(--space-4); max-width: 1280px; margin: 0 auto; }
</style>
