<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'

const skus = ref([])
const tickets = ref([])
const msg = ref('')
const err = ref('')
const fence = ref(null)
const rescheduleMap = reactive({})
const form = reactive({
  title: '',
  effective_at: '2026-11-16T10:00:00Z',
  note: '',
  sku_code: '',
  new_price: '',
})
const lines = ref([])

async function refresh() {
  skus.value = await api.skus()
  tickets.value = await api.tickets()
  fence.value = await api.fence()
  if (!form.sku_code && skus.value.length) form.sku_code = skus.value[0].sku_code
}

function addLine() {
  if (!form.sku_code || form.new_price === '') {
    err.value = '请选择商品并填写新价'
    return
  }
  lines.value.push({ sku_code: form.sku_code, new_price: Number(form.new_price) })
  form.new_price = ''
  err.value = ''
}

function removeLine(i) {
  lines.value.splice(i, 1)
}

async function submit() {
  msg.value = ''
  err.value = ''
  try {
    const t = await api.createTicket({
      title: form.title,
      effective_at: form.effective_at,
      note: form.note || null,
      lines: lines.value,
    })
    if (t.deferred_to_later_batch) {
      msg.value = `已创建 ${t.ticket_no}，应用批次 ${t.lock_batch_no} 持锁中：本单不进本批，留待后续应用`
    } else {
      msg.value = `已创建 ${t.ticket_no}`
    }
    form.title = ''
    form.note = ''
    lines.value = []
    await refresh()
  } catch (e) {
    err.value = e.message
    await refresh()
  }
}

async function reschedule(t) {
  msg.value = ''
  err.value = ''
  const next = (rescheduleMap[t.id] || '').trim()
  if (!next) {
    err.value = '请先填写新的生效时刻'
    return
  }
  try {
    await api.reschedule(t.id, next)
    msg.value = `${t.ticket_no} 已改约至 ${next}`
    rescheduleMap[t.id] = ''
    await refresh()
  } catch (e) {
    err.value = e.message
    await refresh()
  }
}

onMounted(() => refresh().catch((e) => (err.value = e.message)))
</script>

<template>
  <section class="hero">
    <h1>改价单</h1>
    <p>为商品预约新价与生效时刻。未到业务钟前不会写入现行价签。</p>
  </section>

  <div v-if="fence && fence.active" class="panel" style="border-left:4px solid var(--warn,#c98a00)">
    <p class="msg ok" style="margin:0">
      应用批次持锁中，当前策略 <strong>{{ fence.policy }}</strong>：
      <template v-if="fence.policy === 'reject'">新建与改约都会失败，批次结束后才允许。</template>
      <template v-else>可新建（落库 scheduled），但不会进入当前批次，只能留待后续应用；改约一律拒绝。</template>
    </p>
  </div>

  <div class="panel">
    <h2>新建改价单</h2>
    <div class="row">
      <div class="field"><label>标题</label><input v-model="form.title" placeholder="例如：午间茶饮促销售价" /></div>
      <div class="field"><label>生效时刻 (UTC)</label><input v-model="form.effective_at" /></div>
      <div class="field" style="flex:1"><label>备注</label><input v-model="form.note" /></div>
    </div>
    <div class="row">
      <div class="field">
        <label>商品</label>
        <select v-model="form.sku_code">
          <option v-for="s in skus" :key="s.sku_code" :value="s.sku_code">
            {{ s.sku_code }} · {{ s.title }}（现价 {{ s.current_price }}）
          </option>
        </select>
      </div>
      <div class="field"><label>新价</label><input v-model="form.new_price" type="number" step="0.01" /></div>
      <button class="btn ghost" type="button" @click="addLine">加入明细</button>
    </div>
    <table v-if="lines.length">
      <thead><tr><th>SKU</th><th>新价</th><th></th></tr></thead>
      <tbody>
        <tr v-for="(l, i) in lines" :key="i">
          <td>{{ l.sku_code }}</td>
          <td>{{ l.new_price }}</td>
          <td><button class="btn ghost" type="button" @click="removeLine(i)">移除</button></td>
        </tr>
      </tbody>
    </table>
    <div class="row" style="margin-top:12px">
      <button class="btn" type="button" @click="submit">提交改价单</button>
    </div>
    <p v-if="msg" class="msg ok">{{ msg }}</p>
    <p v-if="err" class="msg err">{{ err }}</p>
  </div>

  <div class="panel">
    <h2>改价单列表</h2>
    <table>
      <thead>
        <tr><th>单号</th><th>标题</th><th>生效时刻</th><th>行数</th><th>状态</th><th>已应用批次</th><th>改约（仅待生效）</th></tr>
      </thead>
      <tbody>
        <tr v-for="t in tickets" :key="t.id">
          <td>{{ t.ticket_no }}</td>
          <td>{{ t.title }}</td>
          <td>{{ t.effective_at }}</td>
          <td>{{ t.line_count }}</td>
          <td>
            <span class="tag" :class="t.status === 'applied' ? 'ok' : 'warn'">{{ t.status }}</span>
            <span v-if="t.fence_lock_id && t.status === 'scheduled'" class="tag warn" title="应用持锁期间新建，不进入该批次">
              延后
            </span>
          </td>
          <td>{{ t.apply_batch_id || '—' }}</td>
          <td>
            <template v-if="t.status === 'scheduled'">
              <input
                v-model="rescheduleMap[t.id]"
                :placeholder="t.effective_at"
                style="min-width:200px"
              />
              <button class="btn ghost" type="button" @click="reschedule(t)">改约</button>
            </template>
            <span v-else>—</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
