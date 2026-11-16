<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const clock = ref('')
const batches = ref([])
const msg = ref('')
const err = ref('')
const result = ref(null)
const fence = ref(null)
const events = ref([])
const locks = ref([])
const detail = ref(null)

async function refresh() {
  const c = await api.clock()
  clock.value = c.business_clock || ''
  batches.value = await api.batches()
  fence.value = await api.fence()
  const ev = await api.fenceEvents()
  events.value = ev.events || []
  const lk = await api.fenceLocks()
  locks.value = lk.locks || []
}

async function saveClock() {
  msg.value = ''
  err.value = ''
  try {
    await api.setClock(clock.value)
    msg.value = '业务钟已更新'
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

async function savePolicy(policy) {
  msg.value = ''
  err.value = ''
  try {
    await api.setFencePolicy(policy)
    msg.value = `持锁策略已切换为 ${policy}`
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

async function applyDue() {
  msg.value = ''
  err.value = ''
  result.value = null
  detail.value = null
  try {
    result.value = await api.applyDue()
    msg.value = result.value.applied
      ? `批次 ${result.value.batch_no}：钉扎 ${result.value.pinned_count} 张，处理 ${result.value.ticket_count} 张`
      : result.value.message
    await refresh()
    if (result.value.applied) {
      detail.value = await api.batch(result.value.batch_no)
    }
  } catch (e) {
    err.value = e.message
    await refresh()
  }
}

async function showBatch(batchNo) {
  detail.value = null
  err.value = ''
  try {
    detail.value = await api.batch(batchNo)
  } catch (e) {
    err.value = e.message
  }
}

const policyLabel = {
  reject: '拒绝新建/改约：持锁期间所有建单与改约失败',
  defer: '允许建单但不进本批：新单落库 scheduled，留待后续应用',
}

onMounted(() => refresh().catch((e) => (err.value = e.message)))
</script>

<template>
  <section class="hero">
    <h1>生效应用</h1>
    <p>
      「应用到期」开始时钉扎到期集合（单号列表 + 生效时刻）；钉扎后至本批结束，钉扎外的单即使生效时刻已到也不写入本批价签。
    </p>
  </section>

  <div class="panel">
    <h2>持锁策略（应用期间建单/改约互斥）</h2>
    <div v-if="fence" class="row">
      <div class="field">
        <label>当前策略：{{ fence.policy }}</label>
        <select :value="fence.policy" @change="savePolicy($event.target.value)">
          <option value="defer">defer — 允许建单但不得进入本批</option>
          <option value="reject">reject — 拒绝新建/改约</option>
        </select>
      </div>
      <div class="field" style="flex:1">
        <label>说明</label>
        <input :value="policyLabel[fence.policy]" disabled />
      </div>
      <div class="field">
        <label>锁状态</label>
        <input :value="fence.active ? '持锁中（建单/改约受限）' : '空闲'" disabled />
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>业务钟</h2>
    <div class="row">
      <div class="field" style="flex:1">
        <label>业务钟 (UTC)</label>
        <input v-model="clock" />
      </div>
      <button class="btn ghost" type="button" @click="saveClock">保存业务钟</button>
      <button class="btn warn" type="button" :disabled="fence && fence.active" @click="applyDue">
        应用到期
      </button>
    </div>
    <p v-if="msg" class="msg ok">{{ msg }}</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <pre v-if="result" style="margin-top:12px;font-size:0.82rem;color:var(--muted);white-space:pre-wrap">{{ JSON.stringify(result, null, 2) }}</pre>
  </div>

  <div class="panel" v-if="detail">
    <h2>批次明细 {{ detail.batch_no }}（钉扎集合 = 本批明细）</h2>
    <p style="color:var(--muted)">
      钉扎 {{ detail.pinned_count }} 张 · 实际处理 {{ detail.ticket_count }} 张 · 明细行 {{ detail.line_count }}
    </p>
    <table>
      <thead>
        <tr><th>钉扎序</th><th>单号</th><th>生效时刻（钉扎）</th><th>行数</th></tr>
      </thead>
      <tbody>
        <tr v-for="d in detail.tickets" :key="d.id">
          <td>{{ d.pinned_order }}</td>
          <td>{{ d.ticket_no }}</td>
          <td>{{ d.effective_at }}</td>
          <td>{{ d.line_count }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="panel">
    <h2>应用批次</h2>
    <table>
      <thead>
        <tr><th>批次</th><th>业务钟</th><th>钉扎数</th><th>处理数</th><th>明细行</th><th>状态</th><th>时间</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td>
          <td>{{ b.clock_at }}</td>
          <td>{{ b.pinned_count }}</td>
          <td>{{ b.ticket_count }}</td>
          <td>{{ b.line_count }}</td>
          <td><span class="tag ok">{{ b.status }}</span></td>
          <td>{{ b.created_at }}</td>
          <td><button class="btn ghost" type="button" @click="showBatch(b.batch_no)">明细</button></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="panel">
    <h2>应用锁历史（建单/改约计数可核）</h2>
    <table v-if="locks.length">
      <thead>
        <tr><th>锁</th><th>批次</th><th>策略</th><th>钉扎</th><th>拒绝建单</th><th>延后建单</th><th>拒绝改约</th><th>状态</th><th>释放时刻</th></tr>
      </thead>
      <tbody>
        <tr v-for="l in locks" :key="l.id">
          <td>#{{ l.id }}</td>
          <td>{{ l.batch_no || '—' }}</td>
          <td>{{ l.policy }}</td>
          <td>{{ l.pinned_count }}</td>
          <td>{{ l.rejected_creates }}</td>
          <td>{{ l.deferred_creates }}</td>
          <td>{{ l.rejected_reschedules }}</td>
          <td><span class="tag" :class="l.status === 'active' ? 'warn' : 'ok'">{{ l.status }}</span></td>
          <td>{{ l.released_at || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else style="color:var(--muted)">暂无应用锁</p>
  </div>

  <div class="panel">
    <h2>围栏事件（拒绝/延后可核）</h2>
    <table v-if="events.length">
      <thead>
        <tr><th>时间</th><th>类型</th><th>批次</th><th>单号</th><th>策略</th><th>详情</th></tr>
      </thead>
      <tbody>
        <tr v-for="e in events" :key="e.id">
          <td>{{ e.created_at }}</td>
          <td><span class="tag warn">{{ e.event_type }}</span></td>
          <td>{{ e.batch_no || '—' }}</td>
          <td>{{ e.ticket_no || '—' }}</td>
          <td>{{ e.policy || '—' }}</td>
          <td>{{ e.detail || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else style="color:var(--muted)">暂无围栏事件</p>
  </div>
</template>
