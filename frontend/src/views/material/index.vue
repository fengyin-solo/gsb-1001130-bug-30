<template>
  <section class="page" data-module="material">
    <header class="page-head">
      <div>
        <h2>养护材料管理</h2>
        <p class="page-desc">维护养护材料，围绕材料编号、材料名称、材料类别、规格型号做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记养护材料</button>
        <button class="btn" type="button" @click="exportRows">导出养护材料清单</button>
      </div>
    </header>

    <!-- 物资工作台：除雪回放复核产生的融雪剂发料待办，整组重放后自动回写闭环 -->
    <section class="panel">
      <div class="panel-head">
        <h3>物资工作台 · 除雪回放待办</h3>
        <span class="panel-hint">修正结论整组提交后自动回写数量并闭环，无需手工改单</span>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>作业编号</th><th>材料</th><th>应配数量</th><th>来源</th><th>状态</th><th>备注</th><th>作业</th></tr>
        </thead>
        <tbody>
          <tr v-for="todo in replayTodos" :key="todo.id" :class="{ 'row-done': todo.status === '已回写' }">
            <td>{{ todo.作业编号 }}</td>
            <td>{{ todo.材料名称 }}</td>
            <td>{{ todo.应配数量 }}</td>
            <td>{{ todo.来源 }}</td>
            <td>
              <em class="tag" :class="todo.status === '待办' ? 'warn' : 'ok'">{{ todo.status }}</em>
            </td>
            <td>{{ todo.备注 }}</td>
            <td class="row-actions">
              <RouterLink class="link" :to="`/winter/replay/${todo.winter_id}`">查看回放复核</RouterLink>
            </td>
          </tr>
          <tr v-if="!replayTodos.length">
            <td colspan="7" class="empty-state">暂无除雪回放物资待办</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无养护材料数据，可先登记养护材料</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条养护材料记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/material'
const columns = ["材料编号", "材料名称", "材料类别", "规格型号", "供应商", "进场日期", "存放地点", "材料状态"]
const actions = ["领用材料", "送检材料", "退还不合格"]
const statuses = ["在库", "已领用", "待检测", "不合格"]
const stats = [{"label": "在库材料", "value": 0}, {"label": "待检测材料", "value": 0}, {"label": "不合格材料", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

type ReplayTodo = {
  id: number
  winter_id: number
  作业编号: string
  材料名称: string
  应配数量: string
  status: string
  来源: string
  备注: string
}
const replayTodos = ref<ReplayTodo[]>([])

async function reloadReplayTodos() {
  try {
    const response = await request('/api/material/todos')
    if (!response.ok) return
    const payload = await response.json()
    replayTodos.value = payload.items ?? []
  } catch {
    // 工作台待办读不到时不影响材料台账主表
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '养护材料登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('养护材料动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护材料操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('养护材料列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护材料列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadReplayTodos()
})
</script>
