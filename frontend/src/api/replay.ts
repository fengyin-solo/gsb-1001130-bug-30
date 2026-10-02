/** 除雪车回放接口封装：统一处理 409 乐观锁冲突的可读提示。 */
import { request } from '@/api/client'

const BASE = '/api/winter/replay'

async function call(path: string, init?: RequestInit): Promise<any> {
  const response = await request(`${BASE}${path}`, init)
  if (!response.ok) {
    let detail = ''
    try {
      detail = (await response.json())?.detail ?? ''
    } catch {
      detail = ''
    }
    if (response.status === 409) {
      throw new Error(detail || '事件版本已变化，请刷新后重试（乐观锁冲突）')
    }
    throw new Error(detail || `回放接口返回 ${response.status}`)
  }
  return response.json()
}

function post(path: string, body?: unknown): Promise<any> {
  return call(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

export const replayApi = {
  seedDemo: () => post('/demo'),
  listWorks: (keyword = '') => call(`/works${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''}`),
  getWork: (code: string) => call(`/works/${encodeURIComponent(code)}`),
  calibrate: (code: string, expectedVersion: number, updates: unknown[]) =>
    post(`/works/${encodeURIComponent(code)}/calibrate`, { expected_version: expectedVersion, updates }),
  supplement: (code: string, expectedVersion: number, points: unknown[]) =>
    post(`/works/${encodeURIComponent(code)}/supplement`, { expected_version: expectedVersion, points }),
  nextTrip: (code: string, expectedVersion: number) =>
    post(`/works/${encodeURIComponent(code)}/trips/next`, { expected_version: expectedVersion }),
  dispatch: (code: string, body: { responsible_group: string; support_from: string | null; seat: string }) =>
    post(`/works/${encodeURIComponent(code)}/dispatch`, body),
  snapshot: (code: string, seq: number, snapshotId?: string) =>
    call(`/works/${encodeURIComponent(code)}/snapshots/${seq}${snapshotId ? `?snapshot_id=${encodeURIComponent(snapshotId)}` : ''}`),
  batch: (workCodes: string[]) => post('/batch', { work_codes: workCodes }),
  reviews: () => call('/reviews'),
  markReview: (body: { 作业编号: string; seq: number; passed: boolean; note?: string }) =>
    post('/reviews', body),
  commit: (reviews: { 作业编号: string; seq: number; passed: boolean }[]) =>
    post('/commit', { reviews }),
  migrate: (expectedVersion: number) => post('/migrate', { expected_version: expectedVersion }),
}

export const materialApi = {
  todos: () => request('/api/material/todos').then((r) => r.json()),
  resolveTodo: (id: number) => request(`/api/material/todos/${id}/resolve`, { method: 'POST' }).then((r) => r.json()),
}
