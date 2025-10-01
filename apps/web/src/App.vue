<template>
  <div id="app">
    <NavBar />
    <main class="container">
      <router-view />
    </main>
    <SiteFooter />
  </div>
</template>

<script setup lang="ts">
import NavBar from './components/NavBar.vue'
import SiteFooter from './components/SiteFooter.vue'
</script>

<style>
#app { min-height: 100vh; display: flex; flex-direction: column; }
main.container { flex: 1 1 auto; padding: var(--space-4); max-width: 1200px; margin: 0 auto; }
</style>
