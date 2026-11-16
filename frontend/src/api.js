async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (!res.ok) {
    let detail = await res.text()
    try {
      const j = JSON.parse(detail)
      detail = j.detail || detail
    } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/api/health'),
  dashboard: () => request('/api/dashboard'),
  skus: () => request('/api/skus'),
  tickets: () => request('/api/tickets'),
  ticket: (id) => request(`/api/tickets/${id}`),
  createTicket: (body) => request('/api/tickets', { method: 'POST', body: JSON.stringify(body) }),
  clock: () => request('/api/apply/clock'),
  setClock: (business_clock) =>
    request('/api/apply/clock', { method: 'PUT', body: JSON.stringify({ business_clock }) }),
  applyDue: () => request('/api/apply/due', { method: 'POST' }),
  batches: () => request('/api/apply/batches'),
  batch: (batchNo) => request(`/api/apply/batches/${encodeURIComponent(batchNo)}`),
  fence: () => request('/api/apply/fence'),
  fenceEvents: () => request('/api/apply/fence/events'),
  fenceLocks: () => request('/api/apply/fence/locks'),
  fencePolicy: () => request('/api/apply/fence/policy'),
  setFencePolicy: (policy) =>
    request('/api/apply/fence/policy', { method: 'PUT', body: JSON.stringify({ policy }) }),
  reschedule: (id, effective_at) =>
    request(`/api/tickets/${id}/reschedule`, {
      method: 'PATCH',
      body: JSON.stringify({ effective_at }),
    }),
  priceBook: () => request('/api/prices/book'),
  history: (sku) => request(sku ? `/api/prices/history?sku_code=${encodeURIComponent(sku)}` : '/api/prices/history'),
  reconcile: () => request('/api/reconcile/sales'),
}
