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

    <section class="replay-panel">
      <header class="page-head">
        <div>
          <h3>物资工作台 · 除雪回放待办</h3>
          <p class="page-desc">除雪车回放复核结论一次提交后回写的融雪剂核销/补充待办。</p>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="loadTodos">刷新待办</button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>待办编号</th>
            <th>关联作业</th>
            <th>关联趟次</th>
            <th>作业路段</th>
            <th>责任组</th>
            <th>待办内容</th>
            <th>回放版本</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="String(todo.id)">
            <td>{{ todo.id }}</td>
            <td>{{ todo.关联作业 }}</td>
            <td>{{ todo.关联趟次 }}</td>
            <td>{{ todo.作业路段 ?? '—' }}</td>
            <td>{{ todo.责任组 ?? '—' }}</td>
            <td>{{ todo.待办内容 }}</td>
            <td>{{ todo.回放版本 ?? '—' }}</td>
            <td>
              <span :class="['tag', todo.status === '已核销' ? 'tag-ok' : 'tag-danger']">{{ todo.status }}</span>
            </td>
            <td>
              <button
                v-if="todo.status === '待办'"
                class="link"
                type="button"
                @click="resolveTodo(todo.id)"
              >
                核销
              </button>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="9" class="empty-state">暂无物资待办；在除雪作业详情完成复核提交后自动生成</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { materialApi } from '@/api/replay'

type Row = Record<string, string | number | null>
type Todo = Record<string, any>

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

const todos = ref<Todo[]>([])

async function loadTodos() {
  try {
    const payload = await materialApi.todos()
    todos.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '物资待办读取失败'
  }
}

async function resolveTodo(id: number) {
  errorMessage.value = ''
  try {
    await materialApi.resolveTodo(id)
    await loadTodos()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待办核销失败'
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
  void loadTodos()
})
</script>

<style scoped>
.tag { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }
.tag-ok { background: #e6f6ec; color: #1a7f37; }
.tag-danger { background: #fdecea; color: #c0392b; }
</style>
