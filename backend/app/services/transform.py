"""转换编排业务规则：状态流转、准入校验、依赖变更影响范围与筛选口径。

准入判断复用 ``app.services.admission``，与数据血缘共用同一套版本仲裁和审计口径：

* 只允许 ``草稿 → 编排评审中 → 已启用 → 已停用`` 的顺序推进；
* 启用前必须确认依赖节点存在且已启用、输入表有合法生产者、发布快照无环；
* 已启用节点的依赖字段发生变化时，只把自身与下游影响范围标记为待复核，
  不回头改写已经落库的发布快照；
* 所有写入口都运行在 ``store.transaction()`` 中，失败不会留下半套状态。

实现取舍：采用“写入时固化发布快照 + 读取时按落库快照对账”的方案 2（见 admission 模块
文档）。快照固化保证历史状态可复现、并发写有共同基线；读取对账负责把历史遗留的
“已启用但成环/依赖失效”记录一次性推进到待复核，且不会在每次进入页面时重复写审计。
"""
from __future__ import annotations

import copy
from typing import Any, Sequence

from app.services import admission
from app.store import store

MODULE = "transform"
REQUIRED_FIELDS = ["节点编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "编排评审中", "已启用", "已停用"]
ACTION_RULES = {"提交编排评审": "编排评审中", "启用编排": "已启用", "停用编排": "已停用"}
ACTION_SOURCES = {
    "提交编排评审": {"草稿"},
    "启用编排": {"编排评审中"},
    "停用编排": {"已启用"},
}
NEGATIVE_ACTIONS = ["停用编排"]

# 编辑入口允许维护的业务字段；状态、复核结论、发布快照等由准入内核独占，
# 避免调用方用临时值覆盖已落库记录（冲突一律按落库版本处理）。
EDITABLE_FIELDS = [
    "节点编号",
    "输入表名",
    "输出表名",
    "转换逻辑",
    "执行顺序",
    "依赖节点",
    "失败策略",
    "调度周期",
    "业务域",
    "负责人",
]
SNAPSHOT_FIELDS = set(admission.SNAPSHOT_FIELDS) | {"依赖节点"}


class TransformService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        with store.lock:
            self._reconcile(rows)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._serialize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        with store.lock:
            self._reconcile(store.rows(MODULE))
        return self._serialize(row)

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values.get("节点编号") or "").strip()
        if self._find_by_code(code) is not None:
            return None, []
        with store.transaction():
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update(self._editable_values(values))
            entry["status"] = STATUS_ORDER[0]
            entry["复核状态"] = admission.REVIEW_PASSED
            entry["available"] = False
            entry["pending"] = True
            entry["abnormal"] = False
            entry["版本记录"] = []
            rows.append(entry)
            admission.append_audit(
                table=MODULE,
                target=code,
                change_type="登记",
                before=None,
                after={"节点编号": code, "输入表名": entry.get("输入表名"), "输出表名": entry.get("输出表名")},
                reason="登记转换节点",
            )
        return self._serialize(entry), []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """维护节点字段；临时提交的状态/复核/快照字段一律忽略，按落库状态仲裁。"""
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"转换节点 {entry_id} 不存在或已归档"
            if entry.get("status") == "已停用":
                return None, "转换节点已停用，不能再改写依赖关系"

            changes = self._editable_values(values)
            if not changes:
                return None, "没有可维护的转换节点字段（状态与复核结论不能由调用方直接改写）"

            old_snapshot = admission.effective_snapshot(entry)
            old_status = entry.get("status")
            before = {field: entry.get(field) for field in changes}
            entry.update(changes)

            snapshot_changed = any(
                field in SNAPSHOT_FIELDS and str(before.get(field)) != str(entry.get(field))
                for field in changes
            )
            # 版本/状态字段不在可编辑清单里；即便出现在请求里也只记录，不覆盖落库值。
            attempted_state = {
                key: values.get(key)
                for key in ("status", "复核状态", "节点版本", "发布快照")
                if key in values
            }
            entry["last_attempt_state_conflict"] = attempted_state

            if old_status == "已启用" and snapshot_changed:
                # 发布快照保持旧值，只把影响范围推进到待复核。
                affected = admission.affected_transform_scope(entry, store.rows(MODULE))
                for scope_entry in affected:
                    if scope_entry is entry:
                        reason = f"节点依赖关系在启用后发生变化（{', '.join(sorted(changes))}）"
                    else:
                        reason = f"上游节点 {admission.node_key(entry)} 的依赖关系发生变化"
                    admission.mark_transform_review(
                        [scope_entry],
                        reason=reason,
                        change_type="依赖变更复核",
                    )
                admission.mark_linked_lineage_review(
                    f"转换节点 {admission.node_key(entry)} 依赖关系发生变化", entry
                )
                self._append_version_record(entry, old_snapshot, changes, "依赖变更待复核")
            elif old_status == "已启用":
                entry["复核状态"] = entry.get("复核状态") or admission.REVIEW_PASSED

            admission.append_audit(
                table=MODULE,
                target=admission.node_key(entry),
                change_type="字段更新",
                before=before,
                after={field: entry.get(field) for field in changes},
                reason="维护转换节点字段；状态冲突按落库版本仲裁",
            )
        return self._serialize(entry), "转换节点字段已更新"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"转换节点 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于转换编排可执行范围"
            target = ACTION_RULES[action]
            current = entry.get("status")
            pending_review = entry.get("复核状态") == admission.REVIEW_PENDING

            # 已落库状态优先；请求体里的临时状态/版本/快照不参与判断。
            if current == target and not (target == "已启用" and pending_review):
                return None, f"转换节点已处于「{target}」，重复{action}未被接受（以落库状态为准）"
            sources = set(ACTION_SOURCES[action])
            if target == "已启用" and current == "已启用" and pending_review:
                # 依赖变化后的影响范围仍停在已启用，复核通过后才能恢复可用；
                # 这不是“重复启用”，而是按落库版本重新做一次准入。
                sources.add("已启用")
            if current not in sources:
                return None, (
                    f"动作「{action}」只能从「{'/'.join(sorted(sources))}」发起，"
                    f"当前落库状态为「{current}」"
                )

            if action == "启用编排":
                nodes = store.rows(MODULE)
                self._reconcile(nodes, audit=False)
                allowed, reasons, details = admission.validate_transform_enable(entry, nodes)
                if not allowed:
                    return None, "启用编排被阻止：" + "；".join(reasons)
                before = {
                    "状态": current,
                    "复核状态": entry.get("复核状态"),
                    "发布快照": entry.get("发布快照"),
                }
                entry["发布快照"] = details["发布快照"]
                entry["快照版本"] = details["快照版本"]
                entry["节点版本"] = details["快照版本"]
                entry["status"] = target
                admission.clear_transform_review(entry, reason="启用编排准入通过")
                entry["available"] = True
                entry["pending"] = False
                entry["abnormal"] = False
                entry["复核原因"] = []
                entry["准入校验"] = {
                    "通过": True,
                    "输入表生产者": details.get("输入表生产者"),
                    "依赖节点": details.get("依赖节点"),
                    "执行计划": self._plan_summary(details.get("执行计划") or {}),
                }
                self._append_version_record(
                    entry,
                    before.get("发布快照"),
                    {"发布快照": entry["发布快照"]},
                    "复核通过" if current == "已启用" else "启用发布",
                )
                admission.append_audit(
                    table=MODULE,
                    target=admission.node_key(entry),
                    change_type="状态流转" if current != "已启用" else "复核完成",
                    before=before,
                    after={
                        "状态": target,
                        "复核状态": admission.REVIEW_PASSED,
                        "发布快照": entry.get("发布快照"),
                        "快照版本": entry.get("快照版本"),
                    },
                    reason=f"启用编排准入通过：{details.get('输入表生产者')}",
                )
                return self._serialize(entry), "转换节点已启用"

            if action == "停用编排":
                # 待复核节点也允许停用：停用本身就是阻断可用状态的合法方式。
                before = {"状态": current, "复核状态": entry.get("复核状态")}
                entry["status"] = target
                entry["复核状态"] = admission.REVIEW_PASSED
                entry["review_status"] = admission.REVIEW_PASSED
                entry["available"] = False
                entry["pending"] = False
                entry["abnormal"] = True
                entry["复核原因"] = []
                self._append_version_record(entry, admission.effective_snapshot(entry), {}, "停用")
                # 停用会让下游节点的依赖不可用；受影响范围回到待复核，历史快照保持不变。
                affected = admission.affected_transform_scope(entry, store.rows(MODULE))
                admission.mark_transform_review(
                    [item for item in affected if item is not entry],
                    reason=f"上游依赖节点 {admission.node_key(entry)} 已停用",
                    change_type="依赖停用联动",
                )
                admission.mark_linked_lineage_review(
                    f"转换节点 {admission.node_key(entry)} 已停用", entry
                )
                admission.append_audit(
                    table=MODULE,
                    target=admission.node_key(entry),
                    change_type="状态流转",
                    before=before,
                    after={"状态": target, "复核状态": admission.REVIEW_PASSED},
                    reason="停用编排；依赖方影响范围进入待复核",
                )
                return self._serialize(entry), "转换节点已停用"

            # 提交编排评审：只做顺序推进，不预写发布快照。
            before = {"状态": current}
            entry["status"] = target
            entry["复核状态"] = admission.REVIEW_PENDING
            entry["review_status"] = admission.REVIEW_PENDING
            entry["available"] = False
            entry["pending"] = True
            entry["abnormal"] = False
            admission.append_audit(
                table=MODULE,
                target=admission.node_key(entry),
                change_type="状态流转",
                before=before,
                after={"状态": target, "复核状态": admission.REVIEW_PENDING},
                reason="提交编排评审",
            )
            return self._serialize(entry), "转换节点已提交编排评审"

    # -------------------------------------------------------------- 一致性读取
    def _reconcile(self, rows: Sequence[dict[str, Any]], *, audit: bool = True) -> None:
        """按落库发布快照对账：已启用但依赖失效/输入表无生产者/成环的节点进入待复核。

        复核状态持久化，重复进入页面不会重复显示或重复写审计。
        """
        if not rows:
            return
        plan = admission.build_execution_plan(rows)
        cycle_keys = set(plan.get("cycle") or [])
        for row in rows:
            if row.get("status") != "已启用":
                continue
            if row.get("复核状态") == admission.REVIEW_PENDING:
                continue
            key = admission.node_key(row)
            reasons: list[str] = []
            resolved, missing = admission.resolve_dependencies(row, rows)
            if missing:
                reasons.append(f"依赖节点不存在：{'、'.join(missing)}")
            inactive = [
                f"{admission.node_key(node)}({node.get('status')})"
                for node in resolved
                if not admission.dependency_active(node)[0]
            ]
            if inactive:
                reasons.append(f"依赖节点不可用：{'、'.join(inactive)}")
            if key in cycle_keys:
                reasons.append(f"执行计划成环：{' → '.join(plan.get('cycle') or [])}")
            producer_ok, producer_message = admission.validate_input_producer(row, rows)
            if not producer_ok:
                reasons.append(f"输入表生产者失效：{producer_message}")
            if reasons:
                if audit:
                    admission.mark_transform_review(
                        [row],
                        reason="；".join(reasons),
                        change_type="准入复核",
                    )
                else:
                    row["复核状态"] = admission.REVIEW_PENDING
                    row["复核原因"] = list(row.get("复核原因") or []) + ["；".join(reasons)]

    def _plan_summary(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "节点数": len(plan.get("nodes") or []),
            "边数": len(plan.get("edges") or []),
            "成环": bool(plan.get("cycle")),
            "环路径": plan.get("cycle") or [],
            "问题": plan.get("issues") or [],
        }

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(row)
        # 当前节点的执行计划按“候选已入图”预演；全局计划用于查看整条已发布链路。
        plan = admission.build_execution_plan(store.rows(MODULE), subject=row)
        global_plan = admission.build_execution_plan(store.rows(MODULE))
        pending = data.get("复核状态") == admission.REVIEW_PENDING
        available = bool(data.get("status") == "已启用" and not pending)
        data["available"] = available
        if data.get("status") == "已启用":
            data["可用状态"] = "待复核" if pending else "可用"
        else:
            data["可用状态"] = "不可用"
        data["执行计划"] = self._plan_summary(plan)
        data["执行计划详情"] = {
            "节点": plan.get("nodes"),
            "边": plan.get("edges"),
            "环路径": plan.get("cycle"),
            "顺序冲突": plan.get("order_conflicts"),
            "调度周期冲突": plan.get("schedule_conflicts"),
        }
        data["全局执行计划"] = self._plan_summary(global_plan)
        if data.get("status") in {"草稿", "编排评审中"}:
            allowed, reasons, details = admission.validate_transform_enable(row, store.rows(MODULE))
            data["准入校验"] = {
                "通过": allowed,
                "原因": reasons,
                "输入表生产者": details.get("输入表生产者"),
                "依赖节点": details.get("依赖节点"),
                "发布快照": details.get("发布快照"),
                "执行计划": self._plan_summary(details.get("执行计划") or {}),
            }
        data["关联血缘"] = [
            {
                "id": relation.get("id"),
                "关系编号": relation.get("关系编号"),
                "上游表名": relation.get("上游表名"),
                "下游表名": relation.get("下游表名"),
                "status": relation.get("status"),
                "复核状态": relation.get("复核状态"),
                "available": bool(
                    relation.get("status") == "已确认"
                    and relation.get("复核状态") != admission.REVIEW_PENDING
                ),
                "上游版本": admission.first_value(relation, admission.UPSTREAM_VERSION_KEYS),
                "下游版本": admission.first_value(relation, admission.DOWNSTREAM_VERSION_KEYS),
                "关系版本": admission.first_value(relation, admission.RELATION_VERSION_KEYS),
                "关联方式": relation.get("关联方式"),
            }
            for relation in admission.linked_lineage_entries(row)
        ]
        data["版本记录"] = list(data.get("版本记录") or [])
        return data

    # ------------------------------------------------------------------ 辅助
    def _find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if admission.node_key(row) == code:
                return row
        return None

    def _editable_values(self, values: dict[str, Any]) -> dict[str, Any]:
        return {
            field: values.get(field)
            for field in EDITABLE_FIELDS
            if field in values
        }

    def _append_version_record(
        self,
        entry: dict[str, Any],
        old_snapshot: Any,
        changes: dict[str, Any],
        change_type: str,
    ) -> None:
        records = list(entry.get("版本记录") or [])
        records.append({
            "节点版本": entry.get("节点版本") or entry.get("快照版本") or "",
            "变更类型": change_type,
            "变更字段": sorted(changes),
            "旧发布快照": old_snapshot,
            "新发布快照": entry.get("发布快照"),
            "操作时间": admission.now_text(),
        })
        entry["版本记录"] = records[-20:]
