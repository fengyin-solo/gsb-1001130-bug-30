<template>
  <section class="page replay-page">
    <header class="page-head">
      <div>
        <h2>除雪作业详情 · 轨迹回放</h2>
        <p class="page-desc">
          按作业编号定位错配路段，校准作业时间轴，批量重放趟次并逐条复核；
          游标与事件版本绑定，补录冲突按乐观锁拦截。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn ghost" to="/winter">返回除雪防滑列表</RouterLink>
        <button class="btn" type="button" @click="refresh">刷新</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="refresh">
      <label class="filter-item">
        <span>作业编号</span>
        <input v-model="code" placeholder="如 WINT-0002" />
      </label>
      <button class="btn primary" type="submit">定位作业卡</button>
      <button class="btn" type="button" @click="loadDemo">载入演示轨迹</button>
      <span v-if="detail" class="version-chip">
        事件版本 v{{ detail.事件版本 }} ·
        <span :class="detail.游标有效 ? 'ok' : 'danger'">
          游标{{ detail.游标有效 ? `有效(第${detail.游标.seq}趟)` : '已失效，需重放' }}
        </span>
      </span>
    </form>

    <div v-if="message" class="error-text">{{ message }}</div>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">作业路段</span>
          <strong class="stat-value">{{ detail.作业路段 ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">错配点（漂移/窗外尾点）</span>
          <strong class="stat-value">
            {{ detail.错配点数 }}
            <small>({{ detail.窗内漂移点.length }}/{{ detail.落到窗外的点.length }})</small>
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前版本趟次</span>
          <strong class="stat-value">{{ detail.趟次.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">作业开始责任组</span>
          <strong class="stat-value">{{ detail.派工.责任组 ?? '未派工' }}</strong>
        </article>
      </div>

      <div v-if="detail.错配点数" class="diagnose">
        <h4>错配诊断</h4>
        <ul>
          <li v-for="point in detail.窗内漂移点" :key="`d-${point.时间}`">
            窗内漂移点：{{ point.时间 }} 桩号 {{ point.桩号 ?? '空' }}，设备上报归属「{{ point.设备上报 }}」
          </li>
          <li v-for="point in detail.落到窗外的点" :key="`o-${point.时间}`">
            窗外尾点：{{ point.时间 }} 桩号 {{ point.桩号 ?? '空' }}，已被划到下一作业时间窗
          </li>
        </ul>
      </div>

      <div class="panels">
        <section class="panel">
          <h3>1 · 时间轴与归属段</h3>
          <p class="hint">切段以开始/结束边界围成的时间窗为准，窗内点无视设备上报漂移。</p>
          <table class="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>桩号</th>
                <th>边界</th>
                <th>设备上报归属</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="event in detail.时间轴"
                :key="`${event.时间}-${event.边界 ?? 'point'}`"
                :class="{ boundary: !!event.边界, filled: !!event.桩号补齐 }"
              >
                <td>{{ event.时间 }}</td>
                <td>{{ event.桩号 ?? (event.边界 ? '—' : '空') }}</td>
                <td>{{ boundaryText(event.边界) }}</td>
                <td>{{ event.作业编号 ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
          <h4>本作业归属段</h4>
          <ul class="seg-list">
            <li v-for="(seg, idx) in detail.归属段" :key="idx">
              {{ seg.开始时间 }} → {{ seg.结束时间 }}，{{ seg.点数 }} 点，桩号序列：
              {{ seg.桩号序列.map((v: number | null) => v ?? '空').join(' → ') }}
            </li>
            <li v-if="!detail.归属段.length" class="empty-state">当前时间窗内没有轨迹点</li>
          </ul>
        </section>

        <section class="panel">
          <h3>2 · 校准时间轴（事务 + 乐观锁）</h3>
          <p class="hint">
            原时间用于定位既有事件（留空为新增）；提交时校验事件版本，版本不符整组失败。
          </p>
          <div class="form-grid">
            <label>原时间（可选）<input v-model="calib.original" placeholder="YYYY-MM-DDTHH:mm:ss" /></label>
            <label>时间<input v-model="calib.time" placeholder="YYYY-MM-DDTHH:mm:ss" /></label>
            <label>
              桩号（留空不改）
              <input v-model="calib.stake" placeholder="如 300，或置空" />
            </label>
            <label>
              设备归属
              <input v-model="calib.workCode" :placeholder="code" />
            </label>
            <label>
              边界类型
              <select v-model="calib.boundary">
                <option value="">轨迹点</option>
                <option value="start">作业开始</option>
                <option value="end">作业结束</option>
              </select>
            </label>
          </div>
          <div class="row-actions">
            <button class="btn primary" type="button" @click="submitCalibrate">提交校准（需 v{{ detail.事件版本 }}）</button>
            <button class="btn" type="button" @click="quickCloseWork">一键给本作业补结束边界（尾点前1分钟）</button>
          </div>

          <h3>3 · 并发补录（乐观锁）</h3>
          <div class="form-grid">
            <label>时间<input v-model="supp.time" placeholder="YYYY-MM-DDTHH:mm:ss" /></label>
            <label>桩号<input v-model="supp.stake" placeholder="如 450" /></label>
          </div>
          <button class="btn primary" type="button" @click="submitSupplement">补录一条轨迹点</button>

          <h3>4 · 跨区支援派工</h3>
          <p class="hint">责任组以作业开始时指挥席位记录为准；已有趟次后不能追溯修改。</p>
          <div class="form-grid">
            <label>责任组<input v-model="dispatchForm.group" placeholder="如 支援二组" /></label>
            <label>支援自<input v-model="dispatchForm.from" placeholder="如 相邻片区（可空）" /></label>
            <label>指挥席位<input v-model="dispatchForm.seat" placeholder="指挥席位" /></label>
          </div>
          <button class="btn" type="button" @click="submitDispatch">记录作业开始责任组</button>
        </section>
      </div>

      <section class="panel">
        <h3>5 · 趟次重放与逐条复核</h3>
        <p class="hint">
          游标与事件版本绑定：轨迹更新后旧游标失效，重放从第 1 趟重新开始；
          断点重放命中已有趟次直接返回冻结快照，不重复生成。
        </p>
        <div class="row-actions">
          <button class="btn primary" type="button" @click="replayNext">重放下一趟</button>
          <button class="btn" type="button" @click="batchAll">批量重放全部作业</button>
          <button class="btn" type="button" @click="migrate">存量迁移：空桩号按相邻轨迹补齐</button>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>趟次</th>
              <th>时间区间</th>
              <th>起→终桩号</th>
              <th>点数</th>
              <th>责任组 / 跨区</th>
              <th>快照</th>
              <th>复核</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="trip in detail.趟次" :key="trip.seq">
              <td>第{{ trip.seq }}趟</td>
              <td>{{ trip.开始时间 }} → {{ trip.结束时间 }}</td>
              <td>{{ trip.起点桩号 ?? '空' }} → {{ trip.终点桩号 ?? '空' }}</td>
              <td>{{ trip.轨迹点数 }}</td>
              <td>{{ trip.责任组 }}<small v-if="trip.跨区支援自">（自{{ trip.跨区支援自 }}）</small></td>
              <td><button class="link" type="button" @click="viewSnapshot(trip)">{{ trip.快照编号 }}</button></td>
              <td>
                <span :class="['tag', reviewTagClass(trip.复核状态)]">{{ trip.复核状态 }}</span>
              </td>
              <td class="row-actions">
                <button class="link" type="button" @click="review(trip, true)">通过</button>
                <button class="link" type="button" @click="review(trip, false)">驳回</button>
              </td>
            </tr>
            <tr v-if="!detail.趟次.length">
              <td colspan="8" class="empty-state">当前版本尚无趟次，点击“重放下一趟”或“批量重放”</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="snapshotView" class="panel">
        <h3>历史回放快照（冻结，不随后续重放改变）</h3>
        <p class="hint">{{ snapshotView.快照编号 }} · 冻结于 {{ snapshotView.冻结时间 }} · v{{ snapshotView.事件版本 }}</p>
        <table class="data-table">
          <thead>
            <tr><th>时间</th><th>桩号</th><th>设备上报归属</th></tr>
          </thead>
          <tbody>
            <tr v-for="(p, idx) in snapshotView.轨迹" :key="idx">
              <td>{{ p.时间 }}</td>
              <td>{{ p.桩号 ?? '空' }}</td>
              <td>{{ p.作业编号 }}</td>
            </tr>
          </tbody>
        </table>
        <button class="btn ghost" type="button" @click="snapshotView = null">关闭快照</button>
      </section>

      <section class="panel">
        <h3>6 · 整组提交：复核结论一次回写</h3>
        <p class="hint">任一趟次缺失或未通过则整组失败；通过后回写除雪台账、车辆清单、物资待办。</p>
        <button class="btn primary" type="button" @click="commitAll">提交全部已通过趟次</button>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { replayApi } from '@/api/replay'

type Trip = Record<string, any>

const route = useRoute()
const code = ref(String(route.params.code ?? 'WINT-0002'))
const detail = ref<Record<string, any> | null>(null)
const message = ref('')
const snapshotView = ref<Record<string, any> | null>(null)

const calib = reactive({ original: '', time: '', stake: '', workCode: '', boundary: '' })
const supp = reactive({ time: '', stake: '' })
const dispatchForm = reactive({ group: '', from: '', seat: '指挥A' })

function boundaryText(boundary: string | undefined) {
  if (boundary === 'start') return '作业开始'
  if (boundary === 'end') return '作业结束'
  return '—'
}

function fail(error: unknown) {
  message.value = error instanceof Error ? error.message : '回放操作失败'
}

async function refresh() {
  message.value = ''
  if (!code.value.trim()) return
  try {
    detail.value = await replayApi.getWork(code.value.trim())
  } catch (error) {
    detail.value = null
    fail(error)
  }
}

async function loadDemo() {
  message.value = ''
  try {
    await replayApi.seedDemo()
    code.value = 'WINT-0002'
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function submitCalibrate() {
  if (!detail.value || !calib.time) {
    message.value = '请填写校准后的时间'
    return
  }
  const update: Record<string, any> = {
    原时间: calib.original || undefined,
    时间: calib.time,
    作业编号: calib.workCode || code.value,
  }
  if (calib.stake !== '') update.桩号 = calib.stake === '' ? null : Number(calib.stake)
  if (calib.boundary) update.边界 = calib.boundary
  try {
    await replayApi.calibrate(code.value, detail.value.事件版本, [update])
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function quickCloseWork() {
  if (!detail.value) return
  // 定位本作业最后一个轨迹点，在其之前补结束边界
  const points = detail.value.时间轴.filter((e: Record<string, any>) => !e.边界)
  const own = points.filter((e: Record<string, any>) => e.作业编号 === code.value)
  if (!own.length) {
    message.value = '当前没有本作业的轨迹点'
    return
  }
  const last = new Date(own[own.length - 1].时间)
  last.setSeconds(last.getSeconds() - 1)
  const pad = (value: number) => String(value).padStart(2, '0')
  const iso = `${last.getFullYear()}-${pad(last.getMonth() + 1)}-${pad(last.getDate())}T${pad(last.getHours())}:${pad(last.getMinutes())}:${pad(last.getSeconds())}`
  try {
    await replayApi.calibrate(code.value, detail.value.事件版本, [
      { 时间: iso, 边界: 'end', 作业编号: code.value },
    ])
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function submitSupplement() {
  if (!detail.value || !supp.time) {
    message.value = '补录时间必填'
    return
  }
  try {
    await replayApi.supplement(code.value, detail.value.事件版本, [
      { 时间: supp.time, 桩号: supp.stake === '' ? null : Number(supp.stake) },
    ])
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function submitDispatch() {
  if (!detail.value || !dispatchForm.group) {
    message.value = '责任组必填'
    return
  }
  try {
    await replayApi.dispatch(code.value, {
      responsible_group: dispatchForm.group,
      support_from: dispatchForm.from || null,
      seat: dispatchForm.seat,
    })
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function replayNext() {
  if (!detail.value) return
  try {
    const result = await replayApi.nextTrip(code.value, detail.value.事件版本)
    if (result.完成) message.value = '所有趟次已重放完毕'
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function batchAll() {
  try {
    const all = await replayApi.listWorks('')
    await replayApi.batch(all.items.map((item: Record<string, any>) => item.作业编号))
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function migrate() {
  if (!detail.value) return
  try {
    const result = await replayApi.migrate(detail.value.事件版本)
    message.value = `迁移完成，补齐空桩号 ${result.补齐桩号点} 个，当前 v${result.事件版本}`
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function review(trip: Trip, passed: boolean) {
  try {
    await replayApi.markReview({ 作业编号: trip.作业编号, seq: trip.seq, passed, note: passed ? '核对一致' : '存疑驳回' })
    await refresh()
  } catch (error) {
    fail(error)
  }
}

async function viewSnapshot(trip: Trip) {
  try {
    snapshotView.value = await replayApi.snapshot(trip.作业编号, trip.seq, trip.快照编号)
  } catch (error) {
    fail(error)
  }
}

async function commitAll() {
  if (!detail.value) return
  try {
    const all = await replayApi.reviews()
    const passed = all.items
      .filter((item: Trip) => item.复核状态 === '通过')
      .map((item: Trip) => ({ 作业编号: item.作业编号, seq: item.seq, passed: true }))
    if (!passed.length) {
      message.value = '没有已通过复核的趟次，请先逐条复核'
      return
    }
    const result = await replayApi.commit(passed)
    message.value = `整组提交成功：${result.已提交} 条趟次已回写台账/车辆/物资待办`
    await refresh()
  } catch (error) {
    fail(error)
  }
}

function reviewTagClass(status: string) {
  if (status === '通过') return 'tag-ok'
  if (status === '驳回') return 'tag-danger'
  return ''
}

onMounted(refresh)
</script>

<style scoped>
.version-chip { margin-left: 0.75rem; font-size: 0.9rem; }
.version-chip .ok { color: #1a7f37; }
.version-chip .danger { color: #c0392b; }
.diagnose {
  border: 1px solid #f0c9c4;
  background: #fdf3f2;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
}
.diagnose h4 { margin: 0 0 0.5rem; color: #c0392b; }
.panels { display: grid; grid-template-columns: 1.2fr 1fr; gap: 1rem; }
.panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}
.panel h3 { margin-top: 0; }
.hint { color: #6b7280; font-size: 0.85rem; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 0.75rem; margin-bottom: 0.5rem; }
.form-grid label { font-size: 0.85rem; color: #374151; display: flex; flex-direction: column; gap: 0.25rem; }
.seg-list { padding-left: 1.1rem; font-size: 0.85rem; }
.seg-list li { margin-bottom: 0.25rem; }
tr.boundary { background: #eef4ff; font-weight: 600; }
tr.filled td:nth-child(2) { color: #b45309; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }
.tag-ok { background: #e6f6ec; color: #1a7f37; }
.tag-danger { background: #fdecea; color: #c0392b; }
@media (max-width: 960px) { .panels { grid-template-columns: 1fr; } }
</style>
