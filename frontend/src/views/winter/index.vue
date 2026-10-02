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
          <th>回放一致性</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <RouterLink class="link" :to="`/winter/replay/${row['作业编号']}`">回放详情</RouterLink>
          </td>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无除雪防滑数据，可先登记除雪作业</td>
        </tr>
      </tbody>
    </table>

    <section class="replay-panel">
      <header class="replay-head">
        <div>
          <h3>除雪车回放 · 错配定位</h3>
          <p class="page-desc">按作业编号定位作业卡与轨迹对不上的路段；校准与重放请到作业详情页。</p>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="loadReplay">刷新回放状态</button>
          <button class="btn primary" type="button" @click="loadDemo">载入演示轨迹（复现跨路段尾点错配）</button>
        </div>
      </header>
      <form class="filter-bar" @submit.prevent="loadReplay">
        <label class="filter-item">
          <span>作业编号</span>
          <input v-model="replayKeyword" placeholder="按作业编号定位，如 WINT-0002" />
        </label>
        <button class="btn" type="submit">定位</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>作业编号</th>
            <th>作业路段</th>
            <th>事件版本</th>
            <th>错配点</th>
            <th>窗内漂移</th>
            <th>窗外尾点</th>
            <th>当前趟次</th>
            <th>游标版本</th>
            <th>责任组</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in replayWorks" :key="item.作业编号">
            <td>{{ item.作业编号 }}</td>
            <td>{{ item.作业路段 ?? '—' }}</td>
            <td>v{{ item.事件版本 }}</td>
            <td>
              <span :class="['tag', item.错配点数 ? 'tag-danger' : 'tag-ok']">{{ item.错配点数 }}</span>
            </td>
            <td>{{ item.窗内漂移点 }}</td>
            <td>{{ item.窗外尾点 }}</td>
            <td>{{ item.当前趟次数 }}</td>
            <td>
              <span :class="['tag', item.游标有效 ? 'tag-ok' : 'tag-danger']">
                {{ item.游标有效 ? `v${item.游标.事件版本}` : '已失效' }}
              </span>
            </td>
            <td>{{ item.责任组 ?? '未派工' }}</td>
            <td>
              <RouterLink class="link" :to="`/winter/replay/${item.作业编号}`">进入详情校准/重放</RouterLink>
            </td>
          </tr>
          <tr v-if="!replayWorks.length">
            <td colspan="10" class="empty-state">尚无回放数据，点击右上角“载入演示轨迹”复现问题</td>
          </tr>
        </tbody>
      </table>
      <span v-if="replayMessage" class="error-text">{{ replayMessage }}</span>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条除雪防滑记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { replayApi } from '@/api/replay'

type Row = Record<string, string | number | null>
type ReplayWork = Record<string, any>

const ENDPOINT = '/api/winter'
const columns = ["作业编号", "作业路段", "作业日期", "融雪剂用量", "作业车辆", "作业班组", "路面状况", "作业状态"]
const actions = ["开始作业", "完成作业", "复查确认"]
const statuses = ["待作业", "作业中", "已完成", "待复查"]
const stats = [{"label": "待作业路段", "value": 0}, {"label": "作业中路段", "value": 0}, {"label": "已完成路段", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const replayWorks = ref<ReplayWork[]>([])
const replayKeyword = ref('')
const replayMessage = ref('')

async function loadReplay() {
  replayMessage.value = ''
  try {
    const payload = await replayApi.listWorks(replayKeyword.value.trim())
    replayWorks.value = payload.items ?? []
  } catch (error) {
    replayMessage.value = error instanceof Error ? error.message : '回放状态读取失败'
  }
}

async function loadDemo() {
  replayMessage.value = ''
  try {
    await replayApi.seedDemo()
    await loadReplay()
  } catch (error) {
    replayMessage.value = error instanceof Error ? error.message : '演示轨迹载入失败'
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
  void loadReplay()
})
</script>

<style scoped>
.replay-panel {
  margin-top: 1.5rem;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 1rem;
}
.replay-head { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.5rem; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }
.tag-ok { background: #e6f6ec; color: #1a7f37; }
.tag-danger { background: #fdecea; color: #c0392b; }
</style>
