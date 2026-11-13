import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/lineage'
const columns = [
  "关系编号",
  "上游表名",
  "下游表名",
  "上游字段",
  "下游字段",
  "转换说明",
  "采集批次",
  "关系版本"
]
const actions = [
  "发起解析",
  "确认关系",
  "废弃关系",
  "提交复核"
]
const statuses = [
  "草稿",
  "解析中",
  "已确认",
  "待复核",
  "已废弃"
]

const actionSources: Record<string, string[]> = {
  "发起解析": ["草稿", "待复核"],
  "确认关系": ["解析中", "待复核"],
  "废弃关系": ["草稿", "解析中", "已确认", "待复核"],
  "提交复核": ["已确认"],
}

type Row = Record<string, any>

type Entry = Row & {
  available?: boolean
  可用状态?: string
  复核状态?: string
  复核原因?: string[]
  版本状态?: Row
  确认快照?: Row | null
  确认历史?: Row[]
  字段映射?: Row[]
  关联转换节点?: Row[]
  版本记录?: Row[]
}

function statusBadge(row: Entry) {
  if (row.available) return <span className="badge ok">可用</span>
  if (row.status === '待复核' || row.复核状态 === '待复核') return <span className="badge pending">待复核</span>
  if (row.status === '已废弃') return <span className="badge blocked">已废弃</span>
  return <span className="badge muted">{row.status ?? '不可用'}</span>
}

function isActionAllowed(action: string, row: Entry) {
  return (actionSources[action] ?? []).includes(String(row.status ?? ''))
}

function mappingText(mapping?: Row[], fallbackUp?: unknown, fallbackDown?: unknown) {
  if (mapping && mapping.length > 0) {
    return mapping.map((item) => `${item.上游字段 || '—'} → ${item.下游字段 || '—'}`).join('；')
  }
  if (fallbackUp || fallbackDown) return `${fallbackUp ?? '—'} → ${fallbackDown ?? '—'}`
  return '—'
}

export default function LineageView() {
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
    const relation = searchParams.get('relation')
    if (!relation) return
    setSelectedId((current) => (current === Number(relation) ? current : Number(relation)))
  }, [searchParams])

  const loadDetail = useCallback(async (id: number | null) => {
    if (id == null) {
      setDetail(null)
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${id}`)
      if (!response.ok) throw new Error('血缘关系详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setDetail(null)
      setErrorMessage(error instanceof Error ? error.message : '血缘关系详情读取失败')
    }
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (keyword) params.set('keyword', keyword)
    if (statusFilter) params.set('status', statusFilter)
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('血缘关系列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      await loadDetail(selectedId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据血缘列表读取失败')
    }
  }, [keyword, statusFilter, loadDetail, selectedId])

  useEffect(() => {
    void reload()
  }, [reload])

  function selectEntry(id: number) {
    setSelectedId(id)
    const next = new URLSearchParams(searchParams)
    next.set('relation', String(id))
    setSearchParams(next)
  }

  function clearSelection() {
    setSelectedId(null)
    const next = new URLSearchParams(searchParams)
    next.delete('relation')
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
      const payload = await response.json().catch(() => ({ ok: false, message: '数据血缘接口未返回结论' }))
      if (!response.ok || !payload.ok) {
        // 确认被准入规则阻止时，关系保持原状态；页面展示后端给出的版本仲裁原因。
        setErrorMessage(payload.message ?? '数据血缘动作未生效')
      } else {
        setNotice(payload.message ?? '数据血缘动作已生效')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据血缘操作失败')
    }
  }

  const stats = useMemo(() => {
    const confirmed = rows.filter((row) => row.status === '已确认' && row.available).length
    const parsing = rows.filter((row) => row.status === '解析中').length
    const pending = rows.filter((row) => row.status === '待复核' || row.复核状态 === '待复核').length
    return [
      { label: '血缘关系总数', value: total },
      { label: '可用关系', value: confirmed },
      { label: '解析中关系', value: parsing },
      { label: '待复核', value: pending },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="lineage">
      <header className="page-head">
        <div>
          <h2>数据血缘管理</h2>
          <p className="page-desc">记录数据表之间的血缘关系，追踪字段级别的上下游依赖和转换逻辑。确认只发生在解析完成后，并绑定上下游当前版本。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('血缘关系登记入口尚未接入审批流')}>
            登记血缘关系
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据血缘清单
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
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
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
              <td className="empty-state" colSpan={columns.length + 3}>暂无数据血缘数据，可先登记血缘关系</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <article className="detail-panel">
          <div className="detail-head">
            <h3>{detail.关系编号} 详情</h3>
            <button className="btn ghost" type="button" onClick={clearSelection}>收起</button>
          </div>
          <div className="detail-grid">
            <span>状态：{detail.status}</span>
            <span>可用状态：{detail.可用状态}</span>
            <span>复核状态：{detail.复核状态 ?? '已通过'}</span>
            <span>关系版本：{detail.版本状态?.关系版本 ?? '—'}</span>
            <span>上游版本：{detail.版本状态?.上游版本 ?? '—'}</span>
            <span>下游版本：{detail.版本状态?.下游版本 ?? '—'}</span>
            <span>字段映射：{mappingText(detail.字段映射, detail.上游字段, detail.下游字段)}</span>
            <span>版本漂移：{detail.版本状态?.版本漂移 ? '是' : '否'}</span>
          </div>

          {(detail.复核原因?.length ?? 0) > 0 && (
            <div className="detail-block">
              <h4>复核原因</h4>
              <ul className="detail-list">
                {detail.复核原因?.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </div>
          )}

          <div className="detail-block">
            <h4>确认快照（版本绑定）</h4>
            {detail.确认快照 ? (
              <table className="detail-table">
                <thead><tr><th>关系版本</th><th>上游版本</th><th>下游版本</th><th>转换说明</th><th>采集批次</th><th>确认时间</th><th>版本签名</th></tr></thead>
                <tbody>
                  <tr>
                    <td>{detail.确认快照['关系版本'] ?? '—'}</td>
                    <td>{detail.确认快照['上游版本'] ?? '—'}</td>
                    <td>{detail.确认快照['下游版本'] ?? '—'}</td>
                    <td>{detail.确认快照['转换说明'] ?? '—'}</td>
                    <td>{detail.确认快照['采集批次'] ?? '—'}</td>
                    <td>{detail.确认快照['确认时间'] ?? '—'}</td>
                    <td className="mono">{detail.确认快照['版本签名'] ?? '—'}</td>
                  </tr>
                </tbody>
              </table>
            ) : <p className="page-desc">尚未确认，确认时会固化双方当前版本。</p>}
          </div>

          <div className="detail-block">
            <h4>关联转换节点</h4>
            {(detail.关联转换节点?.length ?? 0) === 0 ? <p className="page-desc">暂无关联转换节点</p> : (
              <table className="detail-table">
                <thead><tr><th>节点编号</th><th>输入表名</th><th>输出表名</th><th>状态</th><th>复核</th><th>快照版本</th><th>关联方式</th><th>跳转</th></tr></thead>
                <tbody>
                  {detail.关联转换节点?.map((node) => (
                    <tr key={String(node.id)}>
                      <td>{node.节点编号}</td>
                      <td>{node.输入表名}</td>
                      <td>{node.输出表名}</td>
                      <td>{node.status}</td>
                      <td>{node.复核状态 ?? '已通过'}</td>
                      <td className="mono">{node.快照版本 ?? '—'}</td>
                      <td>{node.关联方式}</td>
                      <td><Link className="inline-link" to={`/transform?node=${node.id}`}>查看转换节点</Link></td>
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
                <thead><tr><th>变更类型</th><th>关系版本</th><th>上游版本</th><th>下游版本</th><th>操作时间</th></tr></thead>
                <tbody>
                  {detail.版本记录?.map((record, index) => (
                    <tr key={`${record.变更类型}-${index}`}>
                      <td>{record.变更类型}</td>
                      <td className="mono">{record.关系版本 ?? '—'}</td>
                      <td>{record.上游版本 ?? '—'}</td>
                      <td>{record.下游版本 ?? '—'}</td>
                      <td>{record.操作时间}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="detail-block">
            <h4>确认历史</h4>
            {(detail.确认历史?.length ?? 0) === 0 ? <p className="page-desc">暂无历史确认</p> : (
              <table className="detail-table">
                <thead><tr><th>关系版本</th><th>上游版本</th><th>下游版本</th><th>采集批次</th><th>确认时间</th></tr></thead>
                <tbody>
                  {detail.确认历史?.map((snapshot, index) => (
                    <tr key={`${snapshot.版本签名 ?? index}-${index}`}>
                      <td>{snapshot.关系版本 ?? '—'}</td>
                      <td>{snapshot.上游版本 ?? '—'}</td>
                      <td>{snapshot.下游版本 ?? '—'}</td>
                      <td>{snapshot.采集批次 ?? '—'}</td>
                      <td>{snapshot.确认时间 ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据血缘记录</span>
        <span className="error-text">{errorMessage}</span>
        <span className="notice-text">{notice}</span>
      </footer>
    </section>
  )
}
