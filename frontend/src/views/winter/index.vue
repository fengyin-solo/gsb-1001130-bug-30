<template>
  <section class="page" data-module="winter">
    <header class="page-head">
      <div>
        <h2>除雪防滑管理</h2>
        <p class="page-desc">维护除雪作业，围绕作业编号、作业路段、作业日期、融雪剂用量做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记除雪作业</button>
        <button class="btn" type="button" @click="exportRows">导出除雪防滑清单</button>
      </div>
    </header>

    <!-- 除雪车回放修复复现入口 -->
    <section class="replay-strip panel">
      <div class="panel-head">
        <h3>除雪车回放趟次复核</h3>
        <span class="panel-hint">列表页按作业编号定位错配路段，再进作业详情校准时间轴、整组重放</span>
      </div>
      <form class="filter-bar" @submit.prevent="reloadReplay">
        <label class="filter-item">
          <span>作业编号</span>
          <input v-model="replayKeyword" placeholder="如 WINT-0002" />
        </label>
        <label class="filter-item checkbox-item">
          <input type="checkbox" v-model="mismatchedOnly" @change="reloadReplay" />
          <span>只看回放错配</span>
        </label>
        <button class="btn" type="submit">定位错配路段</button>
        <button class="btn ghost" type="button" :disabled="migrating" @click="runMigration">
          存量空桩号迁移（按相邻轨迹补齐）
        </button>
        <span v-if="migrationResult" class="migration-result">
          补齐 {{ migrationResult.filled.length }} 处空桩号，历史快照跳过 {{ migrationResult.skipped_snapshot }} 条
        </span>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>作业编号</th><th>作业路段</th><th>作业车辆</th><th>责任组</th>
            <th>回放问题</th><th>回放状态</th><th>游标/版本</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in replayWorks" :key="item.winter_id" :class="{ 'row-snapshot': item.snapshot }">
            <td>{{ item.作业编号 }}<em v-if="item.snapshot" class="tag">历史快照</em></td>
            <td>{{ item.作业路段 }}</td>
            <td>{{ item.作业车辆 }}</td>
            <td>{{ item.责任组 }}</td>
            <td>
              <span v-if="item.问题汇总.total" class="issue-badge">
                {{ item.问题汇总.total }} 处
                <small>{{ issueText(item.问题汇总.明细) }}</small>
              </span>
              <span v-else class="ok-text">已对齐</span>
            </td>
            <td>{{ item.replay_status }}</td>
            <td>{{ item.replay_cursor ?? '—' }} / v{{ item.event_version }}</td>
            <td class="row-actions">
              <RouterLink class="link" :to="`/winter/replay/${item.winter_id}`">
                {{ item.snapshot ? '查看回放快照' : '进作业详情复核' }}
              </RouterLink>
            </td>
          </tr>
          <tr v-if="!replayWorks.length">
            <td colspan="8" class="empty-state">该条件下没有需要复核的除雪作业</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无除雪防滑数据，可先登记除雪作业</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条除雪防滑记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type ReplayWork = {
  winter_id: number
  作业编号: string
  作业路段: string
  作业车辆: string
  责任组: string
  replay_status: string
  event_version: number
  replay_cursor: number | null
  snapshot: boolean
  问题汇总: { total: number; 明细: Record<string, number> }
}

const ENDPOINT = '/api/winter'
const columns = ['作业编号', '作业路段', '作业日期', '融雪剂用量', '作业车辆', '作业班组', '路面状况', '作业状态']
const actions = ['开始作业', '完成作业', '复查确认']
const stats = [{ label: '待作业路段', value: 0 }, { label: '作业中路段', value: 0 }, { label: '已完成路段', value: 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const replayWorks = ref<ReplayWork[]>([])
const replayKeyword = ref('')
const mismatchedOnly = ref(true)
const migrating = ref(false)
const migrationResult = ref<{ filled: unknown[]; skipped_snapshot: number } | null>(null)

function issueText(detail: Record<string, number>): string {
  return Object.entries(detail).map(([type, count]) => `${type}×${count}`).join('，')
}

async function reloadReplay() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (replayKeyword.value) query.set('keyword', replayKeyword.value.trim())
  query.set('mismatched_only', String(mismatchedOnly.value))
  try {
    const response = await request(`/api/winter/replay/works?${query.toString()}`)
    if (!response.ok) throw new Error('回放错配列表读取失败')
    const payload = await response.json()
    replayWorks.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回放错配列表读取失败'
  }
}

async function runMigration() {
  migrating.value = true
  errorMessage.value = ''
  try {
    const response = await request('/api/winter/replay/migrate-stakes', { method: 'POST' })
    if (!response.ok) throw new Error('存量空桩号迁移失败')
    migrationResult.value = await response.json()
    await reloadReplay()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存量空桩号迁移失败'
  } finally {
    migrating.value = false
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
  errorMessage.value = '除雪作业登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('除雪防滑动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '除雪防滑操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('除雪作业列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '除雪防滑列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadReplay()
})
</script>
