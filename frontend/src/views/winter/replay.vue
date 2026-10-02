<template>
  <section class="page" data-module="winter-replay">
    <header class="page-head">
      <div>
        <h2>除雪作业回放复核 · {{ detail?.作业编号 ?? '加载中' }}</h2>
        <p class="page-desc">
          按作业编号定位错配路段后，先校准作业时间轴，再逐条复核轨迹；多条轨迹整组一次提交，
          修正结论回写除雪台账、车辆清单与物资待办。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn ghost" to="/winter">返回除雪防滑列表</RouterLink>
      </div>
    </header>

    <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">作业路段 / 车辆</span>
          <strong class="stat-value" style="font-size:15px">{{ detail.作业路段 }} · {{ detail.作业车辆 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">作业开始时责任组（跨区支援以此为准）</span>
          <strong class="stat-value" style="font-size:15px">
            {{ detail.责任组 }}<em v-if="detail.cross_area" class="tag warn">跨区支援</em>
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">回放状态 / 游标</span>
          <strong class="stat-value" style="font-size:15px">
            {{ detail.replay_status }} · 游标 {{ detail.replay_cursor ?? '—' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">事件版本（回放游标与之绑定）</span>
          <strong class="stat-value" style="font-size:15px">v{{ detail.event_version }}</strong>
        </article>
      </div>

      <!-- 历史快照作业只读 -->
      <div v-if="detail.snapshot" class="snapshot-banner">
        历史路线保持原回放快照不变：空桩号、旧作业卡均不参与迁移与重放。
      </div>

      <!-- 时间轴校准 -->
      <section class="panel" v-if="!detail.snapshot">
        <div class="panel-head">
          <h3>① 校准作业时间轴</h3>
          <span class="panel-hint">窗口口径用于判定轨迹点是否时间轴错位</span>
        </div>
        <table class="data-table timeline-table">
          <thead>
            <tr><th>路段</th><th>窗口开始</th><th>窗口结束</th></tr>
          </thead>
          <tbody>
            <tr v-for="(seg, index) in timelineDraft" :key="index">
              <td><input v-model="seg.section" /></td>
              <td><input v-model="seg.start" placeholder="YYYY-MM-DD HH:MM:SS" /></td>
              <td><input v-model="seg.end" placeholder="YYYY-MM-DD HH:MM:SS" /></td>
            </tr>
          </tbody>
        </table>
        <div class="panel-actions">
          <button class="btn primary" type="button" :disabled="submitting" @click="saveTimeline">
            保存校准（乐观锁 v{{ detail.event_version }}）
          </button>
        </div>
      </section>

      <!-- 趟次逐条复核 -->
      <section class="panel">
        <div class="panel-head">
          <h3>② 多条轨迹逐条复核</h3>
          <span class="panel-hint">
            勾选参与重放的趟次；重复旧趟次重放时自动去重，断点重放已提交趟次幂等跳过
          </span>
        </div>

        <div v-for="trip in detail.trips" :key="trip.id" class="trip-card" :class="tripClass(trip)">
          <div class="trip-head">
            <label v-if="!trip.snapshot" class="trip-check">
              <input type="checkbox" v-model="selectedTripIds" :value="trip.id" />
            </label>
            <span class="trip-title">第 {{ trip.ordinal }} 趟</span>
            <em class="tag">{{ trip.source }}</em>
            <em class="tag" :class="trip.duplicate_of ? 'warn' : ''">
              {{ trip.duplicate_of ? `重复旧趟次（已被 #${trip.duplicate_of} 取代）` : trip.status }}
            </em>
            <em v-if="trip.snapshot" class="tag">历史快照</em>
          </div>

          <div class="trip-grid">
            <label class="trip-field">
              <span>作业卡路段</span>
              <input v-if="!trip.snapshot" v-model="tripEdits[trip.id].card_section" />
              <b v-else>{{ trip.card_section }}</b>
            </label>
            <label class="trip-field">
              <span>车载记录责任组</span>
              <b>{{ trip.recorded_team }}</b>
            </label>
            <label class="trip-field">
              <span>回放有效责任组</span>
              <b>{{ trip.effective_team }}</b>
            </label>
          </div>

          <!-- 轨迹点（无地图时以桩号线性呈现） -->
          <div class="track-row">
            <template v-for="(point, pIndex) in trip.points" :key="pIndex">
              <span class="track-point" :class="{ filled: point.stake_filled, empty: !point.stake }">
                {{ point.stake || '空桩号' }}
                <small>{{ point.ts.slice(11, 16) }}</small>
              </span>
              <span v-if="pIndex < trip.points.length - 1" class="track-line"></span>
            </template>
          </div>

          <ul v-if="trip.issues.length" class="issue-list">
            <li v-for="(issue, iIndex) in trip.issues" :key="iIndex" :class="issueClass(issue.类型)">
              {{ issue.类型 }}：{{ issue.说明 }}
            </li>
          </ul>
          <div v-else class="issue-ok">该趟次与作业卡、时间轴相符，可整组提交。</div>

          <div class="trip-actions" v-if="!trip.snapshot">
            <button class="link" type="button" :disabled="submitting" @click="saveSupplement(trip)">
              补录本趟（乐观锁）
            </button>
          </div>
        </div>

        <div class="panel-actions batch-bar" v-if="!detail.snapshot">
          <div class="batch-meta">
            已选 {{ selectedTripIds.length }} 趟 ·
            提交游标 {{ detail.replay_cursor ?? '从头' }} · 基于事件版本 v{{ detail.event_version }}
          </div>
          <button class="btn primary" type="button" :disabled="submitting || !selectedTripIds.length" @click="submitReplay">
            {{ detail.replay_status === '断点重放中' ? '断点续放（整组提交）' : '批量重放（整组一次提交）' }}
          </button>
        </div>
      </section>

      <!-- 回写结论 -->
      <section class="panel" v-if="lastWriteback">
        <div class="panel-head"><h3>③ 修正结论回写</h3></div>
        <div class="writeback-grid">
          <div><b>除雪台账</b><pre>{{ formatJson(lastWriteback['除雪台账']) }}</pre></div>
          <div><b>车辆清单</b><pre>{{ formatJson(lastWriteback['车辆清单']) }}</pre></div>
          <div><b>物资待办</b><pre>{{ formatJson(lastWriteback['物资待办']) }}</pre></div>
        </div>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

type Issue = { 类型: string; 说明: string }
type Point = { ts: string; stake: string; stake_filled: boolean }
type Trip = {
  id: number
  ordinal: number
  source: string
  card_section: string
  recorded_team: string
  effective_team: string
  status: string
  snapshot: boolean
  duplicate_of: number | null
  points: Point[]
  issues: Issue[]
}
type WorkDetail = {
  winter_id: number
  作业编号: string
  作业路段: string
  作业车辆: string
  责任组: string
  cross_area: boolean
  event_version: number
  replay_cursor: number | null
  replay_status: string
  snapshot: boolean
  timeline: { section: string; start: string; end: string }[]
  trips: Trip[]
}

const route = useRoute()
const winterId = Number(route.params.id)

const detail = ref<WorkDetail | null>(null)
const timelineDraft = ref<{ section: string; start: string; end: string }[]>([])
const selectedTripIds = ref<number[]>([])
const tripEdits = reactive<Record<number, { card_section: string }>>({})
const submitting = ref(false)
const errorMessage = ref('')
const lastWriteback = ref<Record<string, unknown> | null>(null)

function applyDetail(payload: WorkDetail) {
  detail.value = payload
  timelineDraft.value = payload.timeline.map((seg) => ({ ...seg }))
  for (const trip of payload.trips) {
    tripEdits[trip.id] = { card_section: trip.card_section }
  }
  // 默认勾选所有非快照趟次（含重复旧趟次，用于演示去重）
  selectedTripIds.value = payload.trips.filter((trip) => !trip.snapshot).map((trip) => trip.id)
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`/api/winter/replay/works/${winterId}`)
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(body.detail ?? '回放详情读取失败')
    }
    applyDetail(await response.json())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回放详情读取失败'
  }
}

function conflictMessage(status: number, body: { detail?: string }): string {
  if (status === 409) return `${body.detail ?? '事件版本已变化'}（请刷新复核页后重试）`
  if (status === 400 && typeof body.detail === 'object') {
    const blockers = (body.detail as { blockers?: { trip_id: number }[] }).blockers ?? []
    return `存在 ${blockers.length} 趟未消除的错配，请先校准/补录：趟次 ${blockers.map((b) => `#${b.trip_id}`).join('、')}`
  }
  return typeof body.detail === 'string' ? body.detail : '操作未生效'
}

async function saveTimeline() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`/api/winter/replay/works/${winterId}/timeline`, {
      method: 'PUT',
      body: JSON.stringify({ expected_version: detail.value?.event_version, timeline: timelineDraft.value }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(conflictMessage(response.status, body))
    applyDetail(body)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '时间轴校准失败'
  } finally {
    submitting.value = false
  }
}

async function saveSupplement(trip: Trip) {
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`/api/winter/replay/works/${winterId}/trips/${trip.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        expected_version: detail.value?.event_version,
        card_section: tripEdits[trip.id].card_section,
      }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(conflictMessage(response.status, body))
    applyDetail(body)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补录失败'
  } finally {
    submitting.value = false
  }
}

async function submitReplay() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`/api/winter/replay/works/${winterId}/replay`, {
      method: 'POST',
      body: JSON.stringify({
        expected_version: detail.value?.event_version,
        expected_cursor: detail.value?.replay_cursor,
        trip_ids: selectedTripIds.value,
      }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(conflictMessage(response.status, body))
    lastWriteback.value = body.writeback ?? null
    await reload()
    if (body.warnings?.length) {
      errorMessage.value = ''
      window.alert(`跨区支援责任组已按作业开始时指挥席位口径纠正：\n${body.warnings.join('\n')}`)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量重放失败'
  } finally {
    submitting.value = false
  }
}

function tripClass(trip: Trip): Record<string, boolean> {
  return {
    'trip-bad': trip.issues.some((i) => i.类型 !== '重复旧趟次'),
    'trip-dup': Boolean(trip.duplicate_of),
    'trip-done': trip.status === '已重放' && !trip.duplicate_of,
    'trip-snapshot': trip.snapshot,
  }
}

function issueClass(type: string): string {
  return type === '重复旧趟次' ? 'issue-dup' : 'issue-bad'
}

function formatJson(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2)
}

onMounted(reload)
</script>
