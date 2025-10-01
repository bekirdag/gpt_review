<template>
  <section class="auth">
    <h1>Login</h1>
    <form @submit.prevent="onSubmit">
      <label>Email <input v-model="email" type="email" required /></label>
      <label>Password <input v-model="password" type="password" required /></label>
      <button type="submit">Sign in</button>
    </form>
  </section>
</template>
<script setup lang="ts">
import { ref } from 'vue'
const email = ref('')
const password = ref('')
const onSubmit = async () => {
  // TODO: call API using fetch or axios with __API_BASE__ or import.meta.env.VITE_API_BASE
  console.log('login', email.value)
}
</script>
<style scoped>
.auth label { display:block; margin-bottom: var(--space-3); }
.auth input { width: 100%; padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-2); }
button { padding: var(--space-2) var(--space-3); border-radius: var(--radius-2); }
</style>
