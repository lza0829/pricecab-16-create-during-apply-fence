# 06-pricecab（价签柜）

Pricecab — 改价单按预约时刻生效，现行价签可查，并与销售夹具对账。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）、Proofpack（采样放行）主链路不同。  
不是电商订单/购物车，不是仓库进出库 CRUD，不是考勤 OA。夹具全本地。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5600 |
| API | http://localhost:10600 |

## 主链（0-1）

选商品建改价单（写明生效时刻）→ 拨业务钟或点「应用到期」→ 现行价签更新 → 销售对账看一致/不一致

## 应用围栏（建单与应用互斥）

「应用到期」开始时先**钉扎到期集合**：在一个独立事务里固定到期单的单号列表与生效时刻，
写入持久化应用锁（`apply_locks`）与批次明细（`apply_batch_tickets`）并提交；锁随后对所有
数据库连接可见。钉扎后到本批结束，分两个互斥策略（`PUT /api/apply/fence/policy`）：

| 策略 | 持锁期间新建改价单 | 改约（改生效时刻） |
| --- | --- | --- |
| `reject` | 一律 409 失败，批次结束后才允许，计入 `rejected_creates` | 一律 409，计入 `rejected_reschedules` |
| `defer`（默认） | 可落库为 `scheduled`，但**不得进入本批**，计入 `deferred_creates`，留待后续应用 | 一律 409，计入 `rejected_reschedules` |

保证：

- 钉扎集合外的单，即使生效时刻已到，也不写入本批价签（`price_book` / `price_history`
  只承载钉扎内单号）；
- 批次明细的单号集合等于钉扎集合中实际处理的子集，回执的 `pinned_tickets` 与 `tickets`
  分别给出钉扎集合与实际处理集合，不谎报；
- 拒绝/延后计数与逐单事件（`GET /api/apply/fence/locks`、`/api/apply/fence/events`）可核；
- 处理阶段失败会把批次与锁标记为 `failed`，重启后下一次应用先回收僵锁（标记 `stale`）。

新增/变更接口：`PATCH /api/tickets/{id}/reschedule`、`GET/PUT /api/apply/fence/policy`、
`GET /api/apply/fence`、`GET /api/apply/fence/locks`、`GET /api/apply/fence/events`、
`GET /api/apply/batches/{batch_no}`。

## 测试

```bash
cd backend && pip install -r requirements-dev.txt && python -m pytest tests/
```

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
