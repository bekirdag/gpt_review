<template>
  <header class="navbar">
    <nav class="inner">
      <router-link class="brand" to="/">App</router-link>
      <ul class="links">
        <li><router-link to="/about">About</router-link></li>
        <li><router-link to="/login">Login</router-link></li>
      </ul>
    </nav>
  </header>
</template>
<script setup lang="ts"></script>
<style scoped>
.navbar { position: sticky; top: 0; background: var(--surface-1); border-bottom: 1px solid var(--border); }
.inner { max-width: 1200px; margin: 0 auto; padding: var(--space-2) var(--space-4); display:flex; align-items:center; justify-content: space-between; }
.brand { font-weight: 700; text-decoration: none; }
.links { list-style: none; display:flex; gap: var(--space-3); margin: 0; padding: 0; }
</style>
