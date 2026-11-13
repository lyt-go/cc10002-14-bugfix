import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/transform'
const columns = [
  "节点编号",
  "输入表名",
  "输出表名",
  "转换逻辑",
  "执行顺序",
  "依赖节点",
  "失败策略",
  "调度周期"
]
const actions = [
  "提交编排评审",
  "启用编排",
  "停用编排"
]
const statuses = [
  "草稿",
  "编排评审中",
  "已启用",
  "已停用"
]

const actionSources: Record<string, string[]> = {
  "提交编排评审": ["草稿"],
  "启用编排": ["编排评审中", "已启用"],
  "停用编排": ["已启用"],
}

type Row = Record<string, any>

type Plan = {
  节点数?: number
  边数?: number
  成环?: boolean
  环路径?: string[]
  问题?: string[]
}

type Entry = Row & {
  available?: boolean
  可用状态?: string
  复核状态?: string
  复核原因?: string[]
  执行计划?: Plan
  执行计划详情?: { 节点?: Row[]; 边?: Row[]; 环路径?: string[]; 顺序冲突?: string[]; 调度周期冲突?: string[] }
  准入校验?: { 通过?: boolean; 原因?: string[]; 输入表生产者?: string; 依赖节点?: string[]; 发布快照?: Row }
  关联血缘?: Row[]
  版本记录?: Row[]
  发布快照?: Row
}

function statusBadge(row: Entry) {
  const available = row.available
  const pending = row.复核状态 === '待复核'
  if (available) return <span className="badge ok">可用</span>
  if (pending) return <span className="badge pending">待复核</span>
  if (row.status === '已停用') return <span className="badge blocked">已停用</span>
  return <span className="badge muted">{row.status ?? '不可用'}</span>
}

function isActionAllowed(action: string, row: Entry) {
  const sources = actionSources[action] ?? []
  if (action === '启用编排' && row.status === '已启用' && row.复核状态 === '待复核') return true
  return sources.includes(String(row.status ?? ''))
}

export default function TransformView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [rows, setRows] = useState<Entry[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [keyword, setKeyword] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Entry | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  useEffect(() => {
    const node = searchParams.get('node')
    if (!node) return
    setSelectedId((current) => (current === Number(node) ? current : Number(node)))
  }, [searchParams])


  const loadDetail = useCallback(async (id: number | null) => {
    if (id == null) {
      setDetail(null)
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${id}`)
      if (!response.ok) throw new Error('转换节点详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setDetail(null)
      setErrorMessage(error instanceof Error ? error.message : '转换节点详情读取失败')
    }
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (keyword) params.set('keyword', keyword)
    if (statusFilter) params.set('status', statusFilter)
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('转换节点列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      await loadDetail(selectedId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换编排列表读取失败')
    }
  }, [keyword, statusFilter, loadDetail, selectedId])

  useEffect(() => {
    void reload()
  }, [reload])

  function selectEntry(id: number) {
    setSelectedId(id)
    const next = new URLSearchParams(searchParams)
    next.set('node', String(id))
    setSearchParams(next)
  }

  function clearSelection() {
    setSelectedId(null)
    const next = new URLSearchParams(searchParams)
    next.delete('node')
    setSearchParams(next)
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '转换编排接口未返回结论' }))
      if (!response.ok || !payload.ok) {
        // 准入被阻止时保留原记录，只展示后端给出的具体原因。
        setErrorMessage(payload.message ?? '转换编排动作未生效')
      } else {
        setNotice(payload.message ?? '转换编排动作已生效')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换编排操作失败')
    }
  }

  const stats = useMemo(() => {
    const enabled = rows.filter((row) => row.status === '已启用' && row.available).length
    const reviewing = rows.filter((row) => row.status === '编排评审中').length
    const pending = rows.filter((row) => row.复核状态 === '待复核').length
    return [
      { label: '转换节点总数', value: total },
      { label: '可用节点', value: enabled },
      { label: '编排评审中', value: reviewing },
      { label: '待复核', value: pending },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="transform">
      <header className="page-head">
        <div>
          <h2>转换编排管理</h2>
          <p className="page-desc">编排ETL转换任务的执行顺序和依赖关系，管理节点间的数据流转。启用前会校验依赖、输入表生产者与发布快照无环。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('转换节点登记入口尚未接入审批流')}>
            登记转换节点
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出转换编排清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>{filterFields[0]}</span>
          <input value={keyword} placeholder={`按${filterFields[0]}检索`} onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatusFilter('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>复核</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] || (column === '依赖节点' ? '无' : '—')}</td>)}
              <td>{statusBadge(row)}</td>
              <td>{row.复核状态 === '待复核' ? <span className="badge pending">待复核</span> : <span className="badge ok">已通过</span>}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => selectEntry(row.id)}>详情</button>
                {actions.map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    disabled={!isActionAllowed(action, row)}
                    title={isActionAllowed(action, row) ? '' : '当前状态不允许该动作'}
                    onClick={() => void runAction(action, row)}
                  >
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无转换编排数据，可先登记转换节点</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <article className="detail-panel">
          <div className="detail-head">
            <h3>{detail.节点编号} 详情</h3>
            <button className="btn ghost" type="button" onClick={() => clearSelection()}>收起</button>
          </div>
          <div className="detail-grid">
            <span>状态：{detail.status}</span>
            <span>可用状态：{detail.可用状态}</span>
            <span>复核状态：{detail.复核状态 ?? '已通过'}</span>
            <span>快照版本：{detail.快照版本 ?? '未固化'}</span>
            <span>输入表生产者：{detail.准入校验?.输入表生产者 ?? '—'}</span>
            <span>执行计划：{detail.执行计划?.节点数 ?? 0} 个节点 / {detail.执行计划?.边数 ?? 0} 条边 / {detail.执行计划?.成环 ? '成环' : '无环'}</span>
          </div>

          {(detail.复核原因?.length ?? 0) > 0 && (
            <div className="detail-block">
              <h4>复核原因</h4>
              <ul className="detail-list">
                {detail.复核原因?.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </div>
          )}

          {(detail.准入校验?.原因?.length ?? 0) > 0 && (
            <div className="detail-block">
              <h4>准入未通过原因</h4>
              <ul className="detail-list">
                {detail.准入校验?.原因?.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </div>
          )}

          <div className="detail-block">
            <h4>发布快照</h4>
            {detail.发布快照 ? (
              <table className="detail-table">
                <thead><tr><th>节点标识</th><th>输入表名</th><th>输出表名</th><th>调度周期</th><th>执行顺序</th><th>失败策略</th><th>依赖节点</th></tr></thead>
                <tbody>
                  <tr>
                    <td>{detail.发布快照['节点标识']}</td>
                    <td>{detail.发布快照['输入表名']}</td>
                    <td>{detail.发布快照['输出表名']}</td>
                    <td>{detail.发布快照['调度周期']}</td>
                    <td>{detail.发布快照['执行顺序']}</td>
                    <td>{detail.发布快照['失败策略']}</td>
                    <td>{detail.发布快照['依赖节点'] || '无'}</td>
                  </tr>
                </tbody>
              </table>
            ) : <p className="page-desc">尚未启用，启用准入通过时会固化发布快照。</p>}
          </div>

          {(detail.执行计划详情?.环路径?.length ?? 0) > 0 && (
            <div className="detail-block">
              <h4>执行计划环路径</h4>
              <p className="mono">{detail.执行计划详情?.环路径?.join(' → ')}</p>
            </div>
          )}

          <div className="detail-block">
            <h4>关联血缘关系</h4>
            {(detail.关联血缘?.length ?? 0) === 0 ? <p className="page-desc">暂无关联血缘</p> : (
              <table className="detail-table">
                <thead><tr><th>关系编号</th><th>上游表名</th><th>下游表名</th><th>状态</th><th>复核</th><th>上游版本</th><th>下游版本</th><th>关联方式</th><th>跳转</th></tr></thead>
                <tbody>
                  {detail.关联血缘?.map((relation) => (
                    <tr key={String(relation.id)}>
                      <td>{relation.关系编号}</td>
                      <td>{relation.上游表名}</td>
                      <td>{relation.下游表名}</td>
                      <td>{relation.status}</td>
                      <td>{relation.复核状态 ?? '已通过'}</td>
                      <td>{relation.上游版本 ?? '—'}</td>
                      <td>{relation.下游版本 ?? '—'}</td>
                      <td>{relation.关联方式}</td>
                      <td><Link className="inline-link" to={`/lineage?relation=${relation.id}`}>查看血缘</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="detail-block">
            <h4>版本记录</h4>
            {(detail.版本记录?.length ?? 0) === 0 ? <p className="page-desc">暂无版本记录</p> : (
              <table className="detail-table">
                <thead><tr><th>变更类型</th><th>变更字段</th><th>节点版本</th><th>操作时间</th></tr></thead>
                <tbody>
                  {detail.版本记录?.map((record, index) => (
                    <tr key={`${record.变更类型}-${index}`}>
                      <td>{record.变更类型}</td>
                      <td>{Array.isArray(record.变更字段) ? record.变更字段.join('、') : record.变更字段}</td>
                      <td className="mono">{record.节点版本 || '—'}</td>
                      <td>{record.操作时间}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条转换编排记录</span>
        <span className="error-text">{errorMessage}</span>
        <span className="notice-text">{notice}</span>
      </footer>
    </section>
  )
}
