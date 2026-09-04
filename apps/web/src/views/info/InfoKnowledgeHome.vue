<template><InfoKnowledgeBases :sources="store.sources" @profile="router.push('/profile')" @open-source="openSource" /></template>
<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import InfoKnowledgeBases from '@/components/InfoKnowledgeBases.vue'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'
const router = useRouter(); const store = useInfoKnowledgeStore()
function openSource(key: string) { router.push(`/knowledge/${key}`) }
onMounted(() => { void store.ensureSources() })
</script>
