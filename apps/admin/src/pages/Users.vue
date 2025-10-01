<template>
  <section>
    <h1>Users</h1>
    <DataTable :columns="columns" :rows="rows" />
  </section>
</template>
<script setup lang="ts">
import { reactive } from 'vue'
import DataTable from '../components/DataTable.vue'
const columns = ['id','name','email','role']
const rows = reactive([
  { id: 1, name: 'Jane', email: 'jane@example.com', role: 'admin' },
  { id: 2, name: 'Ali', email: 'ali@example.com', role: 'editor' }
])
</script>
