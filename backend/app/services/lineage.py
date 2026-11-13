"""数据血缘业务规则：状态流转、版本绑定、复核仲裁与筛选口径。

与转换编排共用 ``app.services.admission`` 的准入内核：

* 只有 ``草稿 → 解析中 → 已确认`` 的顺序推进；确认必须发生在解析完成之后；
* 确认时把上下游字段映射、转换说明、采集批次和双方当前版本写入 ``确认快照``；
* 确认后任一侧版本变化，关系立即进入 ``待复核``，旧确认快照保留为历史；
* 并发确认同一关系只接受一次，失败时整库回滚，不留下半套字段关系。

实现取舍同转换编排：确认快照在写入时固化、读取时按落库版本对账。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Sequence

from app.services import admission
from app.store import store

MODULE = "lineage"
REQUIRED_FIELDS = ["关系编号", "上游表名", "下游表名"]
REQUIRED_CONFIRM_FIELDS = ["上游字段", "下游字段", "转换说明", "采集批次"]
STATUS_ORDER = ["草稿", "解析中", "已确认", "待复核", "已废弃"]
ACTION_RULES = {
    "发起解析": "解析中",
    "确认关系": "已确认",
    "废弃关系": "已废弃",
    "提交复核": "待复核",
}
ACTION_SOURCES = {
    "发起解析": {"草稿", "待复核"},
    "确认关系": {"解析中", "待复核"},
    "废弃关系": {"草稿", "解析中", "已确认", "待复核"},
    "提交复核": {"已确认"},
}
NEGATIVE_ACTIONS = ["废弃关系", "提交复核"]

EDITABLE_FIELDS = [
    "关系编号",
    "上游表名",
    "下游表名",
    "上游字段",
    "下游字段",
    "转换说明",
    "采集批次",
    "关系版本",
    "上游版本",
    "下游版本",
    "字段映射",
    "上下游字段映射",
    "上下游版本",
    "解析批次",
    "解析完成",
    "解析结果",
]
PROTECTED_FIELDS = {
    "status",
    "状态",
    "复核状态",
    "review_status",
    "available",
    "可用状态",
    "确认快照",
    "确认历史",
    "版本记录",
}

CONFIRM_MAPPING_FIELDS = ("字段映射", "上下游字段映射")
VERSION_SIDE_KEYS = {
    "up": admission.UPSTREAM_VERSION_KEYS,
    "down": admission.DOWNSTREAM_VERSION_KEYS,
}
CONFIRM_FIELDS = ("上游字段", "下游字段", "转换说明", "采集批次", "关系版本")


class LineageService:
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
            rows = [row for row in rows if keyword in str(row.get("关系编号", ""))]
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
        code = str(values.get("关系编号") or "").strip()
        if self._find_by_code(code) is not None:
            return None, []
        with store.transaction():
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in EDITABLE_FIELDS if field in values})
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
                after={"关系编号": code, "上游表名": entry.get("上游表名"), "下游表名": entry.get("下游表名")},
                reason="登记血缘关系",
            )
        return self._serialize(entry), []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """维护字段/版本；临时提交的状态与确认快照被忽略，按落库版本仲裁。"""
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"血缘关系 {entry_id} 不存在或已归档"
            if entry.get("status") == "已废弃":
                return None, "血缘关系已废弃，不能再改写字段关系"

            changes = {field: values.get(field) for field in EDITABLE_FIELDS if field in values}
            if not changes:
                return None, "没有可维护的血缘字段（状态与确认快照不能由调用方直接改写）"

            old_status = entry.get("status")
            before = {field: entry.get(field) for field in changes}
            entry.update(changes)
            self._refresh_version_fields(entry, values)
            attempted_state = {
                key: values.get(key)
                for key in ("status", "复核状态", "确认快照")
                if key in values
            }
            entry["last_attempt_state_conflict"] = attempted_state

            drift, drift_reason = admission.version_drift(entry)
            if old_status == "已确认" and (drift or self._relation_fields_touched(changes)):
                reason = drift_reason or "上下游字段关系或转换说明发生变化"
                admission.mark_lineage_review(entry, reason=reason, change_type="字段变更复核")
            elif old_status == admission.REVIEW_PENDING:
                entry["复核状态"] = admission.REVIEW_PENDING
                entry["available"] = False

            admission.append_audit(
                table=MODULE,
                target=str(entry.get("关系编号") or entry.get("id")),
                change_type="字段更新",
                before=before,
                after={field: entry.get(field) for field in changes},
                reason="维护血缘字段；状态冲突按落库版本仲裁",
            )
        return self._serialize(entry), "血缘关系字段已更新"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"血缘关系 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据血缘可执行范围"
            target = ACTION_RULES[action]
            current = entry.get("status")

            if current == target:
                return None, f"血缘关系已处于「{target}」，重复{action}未被接受（以落库状态为准）"
            if current not in ACTION_SOURCES[action]:
                return None, (
                    f"动作「{action}」只能从「{'/'.join(sorted(ACTION_SOURCES[action]))}」发起，"
                    f"当前落库状态为「{current}」"
                )

            if action == "确认关系":
                drift, drift_reason = admission.version_drift(entry)
                if drift:
                    admission.mark_lineage_review(
                        entry,
                        reason=f"确认被阻止：{drift_reason}",
                        change_type="确认仲裁",
                    )
                    return None, f"确认关系被阻止：{drift_reason}；结果已标记为待复核"
                # 确认只能发生在解析完成之后；落库状态是“解析中”且已有解析批次才算完成。
                if not self._parsed(entry):
                    return None, "确认关系被阻止：解析尚未完成，缺少可绑定的解析批次"
                mapping = self._confirmation_mapping(entry)
                if mapping is None:
                    return None, "确认关系被阻止：缺少上下游字段映射"
                missing = [
                    field for field in REQUIRED_CONFIRM_FIELDS
                    if not str(entry.get(field) or "").strip()
                ]
                if missing:
                    return None, f"确认关系被阻止：缺少{'、'.join(missing)}"

                snapshot = self._build_confirmation_snapshot(entry, mapping)
                if self._is_duplicate_confirmation(entry, snapshot):
                    return None, "确认关系未被接受：同一关系版本已确认过（以落库版本为准）"

                before = {"状态": current, "确认快照": admission.confirmation_binding(entry)}
                entry["status"] = target
                entry["确认快照"] = snapshot
                entry["确认历史"] = list(entry.get("确认历史") or []) + [snapshot]
                entry["字段映射"] = mapping
                if not entry.get("关系版本"):
                    entry["关系版本"] = snapshot.get("关系版本")
                admission.mark_lineage_result(entry, reason="确认关系：版本绑定已固化")
                self._append_version_record(entry, before.get("确认快照"), snapshot, "确认关系")
                return self._serialize(entry), "血缘关系已确认"

            if action == "发起解析":
                before = {"状态": current}
                entry["status"] = target
                entry["复核状态"] = admission.REVIEW_PENDING
                entry["review_status"] = admission.REVIEW_PENDING
                entry["available"] = False
                entry["pending"] = True
                entry["abnormal"] = False
                # 新解析轮次：清掉旧解析批次标记，确认必须等待本轮解析完成。
                entry["解析批次"] = ""
                entry["解析完成"] = False
                admission.append_audit(
                    table=MODULE,
                    target=str(entry.get("关系编号") or entry.get("id")),
                    change_type="状态流转",
                    before=before,
                    after={"状态": target},
                    reason="发起解析",
                )
                return self._serialize(entry), "血缘关系已发起解析"

            if action == "废弃关系":
                before = {"状态": current, "确认快照": admission.confirmation_binding(entry)}
                entry["status"] = target
                admission.mark_lineage_result(entry, reason="废弃关系；历史确认快照保留")
                entry["available"] = False
                entry["pending"] = False
                entry["abnormal"] = True
                self._append_version_record(entry, before.get("确认快照"), {}, "废弃关系")
                admission.append_audit(
                    table=MODULE,
                    target=str(entry.get("关系编号") or entry.get("id")),
                    change_type="状态流转",
                    before=before,
                    after={"状态": target},
                    reason="废弃关系；历史确认快照保留",
                )
                admission.mark_linked_transform_review(
                    f"血缘关系 {entry.get('关系编号')} 已废弃", entry
                )
                return self._serialize(entry), "血缘关系已废弃"

            # 提交复核：已确认关系主动回到待复核，旧确认历史保留。
            drift, drift_reason = admission.version_drift(entry)
            reason = drift_reason or "人工提交复核"
            admission.mark_lineage_review(entry, reason=reason, change_type="提交复核")
            return self._serialize(entry), "血缘关系已提交复核"

    # -------------------------------------------------------------- 一致性读取
    def _reconcile(self, rows: Sequence[dict[str, Any]]) -> None:
        """读取前按落库版本对账：已确认但版本漂移/缺少确认绑定的关系进入待复核。"""
        for row in rows:
            if row.get("status") != "已确认":
                continue
            if row.get("复核状态") == admission.REVIEW_PENDING:
                continue
            drift, drift_reason = admission.version_drift(row)
            if drift:
                admission.mark_lineage_review(row, reason=drift_reason, change_type="版本漂移")
                admission.mark_linked_transform_review(
                    f"血缘关系 {row.get('关系编号')} 版本漂移", row
                )
            elif not admission.confirmation_binding(row):
                # 历史数据没有确认快照：不允许继续显示为可用，回到待复核等待重新确认。
                admission.mark_lineage_review(
                    row,
                    reason="缺少确认时固化的版本绑定，需要重新确认",
                    change_type="准入复核",
                )

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(row)
        binding = admission.confirmation_binding(row)
        drift, drift_reason = admission.version_drift(row)
        pending = data.get("复核状态") == admission.REVIEW_PENDING or data.get("status") == admission.REVIEW_PENDING
        available = bool(data.get("status") == "已确认" and binding and not pending and not drift)
        data["available"] = available
        data["可用状态"] = "可用" if available else ("待复核" if pending else "不可用")
        data["版本状态"] = {
            "上游版本": admission.first_value(row, admission.UPSTREAM_VERSION_KEYS),
            "下游版本": admission.first_value(row, admission.DOWNSTREAM_VERSION_KEYS),
            "关系版本": admission.first_value(row, admission.RELATION_VERSION_KEYS),
            "绑定上游版本": (binding or {}).get("上游版本"),
            "绑定下游版本": (binding or {}).get("下游版本"),
            "绑定关系版本": (binding or {}).get("关系版本"),
            "版本漂移": drift,
            "漂移说明": drift_reason,
        }
        data["确认快照"] = binding
        data["确认历史"] = list(data.get("确认历史") or [])
        data["字段映射"] = data.get("字段映射") or (binding or {}).get("字段映射")
        data["关联转换节点"] = [
            {
                "id": node.get("id"),
                "节点编号": node.get("节点编号"),
                "输入表名": node.get("输入表名"),
                "输出表名": node.get("输出表名"),
                "status": node.get("status"),
                "复核状态": node.get("复核状态"),
                "available": bool(
                    node.get("status") == "已启用"
                    and node.get("复核状态") != admission.REVIEW_PENDING
                ),
                "快照版本": node.get("快照版本"),
                "关联方式": node.get("关联方式"),
            }
            for node in admission.linked_transform_entries(row)
        ]
        data["版本记录"] = list(data.get("版本记录") or [])
        return data

    # ------------------------------------------------------------------ 辅助
    def _find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("关系编号") or "").strip() == code:
                return row
        return None

    def _parsed(self, entry: dict[str, Any]) -> bool:
        """解析完成：本轮解析（发起解析时清空批次）已经回填解析批次或解析完成标记。

        草稿阶段遗留的采集批次不算解析结果，避免“跳过解析直接确认”。
        """
        if entry.get("解析完成") is True:
            return True
        return bool(str(entry.get("解析批次") or "").strip())

    def _confirmation_mapping(self, entry: dict[str, Any]) -> list[dict[str, str]] | None:
        for field in CONFIRM_MAPPING_FIELDS:
            mapping = admission.normalise_mapping(entry.get(field))
            if mapping:
                return mapping
        upstream_field = str(entry.get("上游字段") or "").strip()
        downstream_field = str(entry.get("下游字段") or "").strip()
        if upstream_field and downstream_field:
            return [{"上游字段": upstream_field, "下游字段": downstream_field}]
        return None

    def _version_state(self, entry: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        relation_version = admission.first_value(entry, admission.RELATION_VERSION_KEYS)
        for side, keys in VERSION_SIDE_KEYS.items():
            explicit = admission.first_value(entry, keys)
            if explicit is not None:
                state[side] = str(explicit).strip()
            else:
                state[side] = str(relation_version).strip() if relation_version else ""
        return state

    def _build_confirmation_snapshot(self, entry: dict[str, Any], mapping: list[dict[str, str]]) -> dict[str, Any]:
        versions = self._version_state(entry)
        snapshot = {
            "关系编号": entry.get("关系编号"),
            "上游表名": entry.get("上游表名"),
            "下游表名": entry.get("下游表名"),
            "上游版本": versions["up"],
            "下游版本": versions["down"],
            "关系版本": str(admission.first_value(entry, admission.RELATION_VERSION_KEYS) or ""),
            "字段映射": mapping,
            "上游字段": entry.get("上游字段"),
            "下游字段": entry.get("下游字段"),
            "转换说明": entry.get("转换说明"),
            "采集批次": entry.get("采集批次"),
            "解析批次": entry.get("解析批次"),
            "确认时间": admission.now_text(),
        }
        snapshot["版本签名"] = self._signature(snapshot)
        return snapshot

    def _signature(self, snapshot: dict[str, Any]) -> str:
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def _is_duplicate_confirmation(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        if entry.get("status") != "已确认":
            return False
        binding = admission.confirmation_binding(entry)
        if not binding:
            return False
        current_up = str(snapshot.get("上游版本") or "")
        current_down = str(snapshot.get("下游版本") or "")
        if current_up == str(binding.get("上游版本") or "") and current_down == str(binding.get("下游版本") or ""):
            return True
        signature = hashlib.sha256(json.dumps({
            "上游版本": current_up,
            "下游版本": current_down,
            "关系版本": snapshot.get("关系版本"),
            "采集批次": snapshot.get("采集批次"),
            "字段映射": snapshot.get("字段映射"),
        }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        return signature in {item.get("版本签名") for item in (entry.get("确认历史") or [])}

    def _refresh_version_fields(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """支持用“上下游版本”整体提交，拆到各自一侧；不覆盖未提交的一侧。"""
        pair = values.get("上下游版本")
        if isinstance(pair, dict):
            up = pair.get("上游版本", pair.get("上游"))
            down = pair.get("下游版本", pair.get("下游"))
            if up is not None:
                entry["上游版本"] = up
            if down is not None:
                entry["下游版本"] = down
        elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
            entry["上游版本"], entry["下游版本"] = pair[0], pair[1]

        for field in ("上游版本", "下游版本", "关系版本"):
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)

    def _relation_fields_touched(self, changes: dict[str, Any]) -> bool:
        tracked = set(CONFIRM_FIELDS) | set(VERSION_SIDE_KEYS["up"]) | set(VERSION_SIDE_KEYS["down"]) | set(CONFIRM_MAPPING_FIELDS) | {"上下游版本"}
        return any(field in tracked for field in changes)

    def _append_version_record(
        self,
        entry: dict[str, Any],
        old_snapshot: Any,
        changes: dict[str, Any],
        change_type: str,
    ) -> None:
        records = list(entry.get("版本记录") or [])
        records.append({
            "变更类型": change_type,
            "旧确认快照": old_snapshot,
            "新确认快照": changes if changes else entry.get("确认快照"),
            "关系版本": entry.get("关系版本"),
            "上游版本": admission.first_value(entry, admission.UPSTREAM_VERSION_KEYS),
            "下游版本": admission.first_value(entry, admission.DOWNSTREAM_VERSION_KEYS),
            "操作时间": admission.now_text(),
        })
        entry["版本记录"] = records[-20:]
