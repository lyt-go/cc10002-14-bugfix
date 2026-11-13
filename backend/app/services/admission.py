"""转换编排与数据血缘共用的准入内核。

两个业务面各有自己的状态机，但“能不能进入可用状态”的判断只在这里实现一次：
依赖解析、输入表生产者校验、发布快照与无环检查、版本仲裁、复核标记和审计写入。

实现策略取舍（两种候选方案）：

1. 读时实时推导：每次列表/详情都从各节点当前字段现算依赖图和版本关系。
   优点是没有冗余数据、字段改动立即可见；缺点是历史状态无法复现，并发写时
   两个请求可能基于不同快照各自判定“通过”，失败后也容易留下半套写入。
2. 写入时固化发布快照 + 版本绑定：准入通过时把节点标识、输入输出表、调度周期、
   执行顺序、失败策略写入 ``发布快照``；血缘确认时写入双方版本绑定。
   后续字段变化只产生“待复核”，不回头改写已落库快照。

这里选择方案 2：它满足“历史关系保留”“冲突以落库版本仲裁”“失败可回滚”
三个硬约束，代价是需要维护快照与版本字段。所有写入口都包在 store.transaction()
里，校验失败时整库回滚，不会出现只落一半节点/字段关系的情况。

读时仍会用已落库快照做一次一致性对账：历史遗留的“已启用但依赖失效/成环”
或“已确认但版本漂移”记录，第一次被读取时会推进到待复核并写一条审计；
因为复核状态是持久化的，重复进入页面不会重复显示或重复写审计。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from app.store import store

# ---------------------------------------------------------------------------
# 公共常量
# ---------------------------------------------------------------------------

TRANSFORM_STATUSES = ("草稿", "编排评审中", "已启用", "已停用")
LINEAGE_STATUSES = ("草稿", "解析中", "已确认", "待复核", "已废弃")

REVIEW_PENDING = "待复核"
REVIEW_PASSED = "已通过"

SNAPSHOT_FIELDS = ("节点标识", "输入表名", "输出表名", "调度周期", "执行顺序", "失败策略")

ACTIVE_DEPENDENCY_STATUS = "已启用"

# 表目录：输入表如果没有转换节点产出，至少要在这些分层表里登记且处于可用状态。
TABLE_MODULES: dict[str, dict[str, Any]] = {
    "landing": {"name_fields": ("落地路径", "批次编号"), "active": ("已落地",)},
    "ods": {"name_fields": ("表名称", "表编号"), "active": ("已上线",)},
    "dwd": {"name_fields": ("表名称", "表编号"), "active": ("已上线",)},
    "dim": {"name_fields": ("维度名称", "维度编号"), "active": ("已上线",)},
    "dws": {"name_fields": ("表名称", "表编号"), "active": ("已上线",)},
    "ads": {"name_fields": ("表名称", "表编号"), "active": ("已上线",)},
}

EMPTY_DEP_TOKENS = {"", "-", "—", "无", "none", "null", "[]", "{}"}


# ---------------------------------------------------------------------------
# 时间与审计
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _json_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def append_audit(
    *,
    table: str,
    target: str,
    change_type: str,
    before: Any,
    after: Any,
    reason: str,
    operator: str | None = None,
) -> dict[str, Any]:
    """把一次准入/状态/版本变化写入变更审计。

    审计本身也是事务的一部分：调用方在写入失败时回滚，不会留下孤立的“已变更”记录。
    """
    rows = store.rows("auditlog")
    next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    record = {
        "id": next_id,
        "status": "已记录",
        "pending": False,
        "abnormal": False,
        "记录编号": f"AUDI-{next_id:04d}",
        "操作表名": table,
        "变更类型": change_type,
        "变更前值": _json_value(before),
        "变更后值": _json_value(after),
        "操作人": operator or "值班管理员",
        "操作时间": now_text(),
        "变更原因": reason,
        "操作对象": target,
    }
    rows.append(record)
    return record


# ---------------------------------------------------------------------------
# 依赖与发布快照
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_dependency_tokens(value: Any) -> list[str]:
    """把“依赖节点”的多种写法拆成引用列表。

    支持 ``"TRAN_DWD,TRAN_ODS"``、``"TRAN_DWD、TRAN_ODS"``、列表以及空值/“无”。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_tokens = [str(item).strip() for item in value]
    else:
        text = str(value).strip()
        if text.lower() in EMPTY_DEP_TOKENS:
            return []
        raw_tokens = [token.strip() for token in re.split(r"[,，;；、\s]+", text) if token.strip()]
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip().strip("[]'\"")
        if token.lower() in EMPTY_DEP_TOKENS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def node_key(entry: dict[str, Any]) -> str:
    """节点在发布快照里的稳定标识：优先业务编号，回落到内部 id。"""
    code = _clean_text(entry.get("节点编号"))
    return code or str(entry.get("id", ""))


def _node_lookup(entries: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for key in {node_key(entry), _clean_text(entry.get("节点编号")), str(entry.get("id", ""))}:
            key = key.strip()
            if not key:
                continue
            lookup.setdefault(key, entry)
            lookup.setdefault(key.upper(), entry)
    return lookup


def resolve_dependencies(
    entry: dict[str, Any], entries: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """解析依赖引用，返回 (已解析节点, 无法解析的原始引用)。"""
    lookup = _node_lookup(entries)
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for token in parse_dependency_tokens(entry.get("依赖节点")):
        target = lookup.get(token) or lookup.get(token.upper()) or lookup.get(str(token))
        if target is None:
            missing.append(token)
            continue
        if target not in resolved:
            resolved.append(target)
    return resolved, missing


def dependency_active(entry: dict[str, Any]) -> tuple[bool, str]:
    """依赖节点必须已落库启用，且自身没有待复核结论。"""
    if entry.get("status") != ACTIVE_DEPENDENCY_STATUS:
        return False, _clean_text(entry.get("status")) or "状态缺失"
    if entry.get("复核状态") == REVIEW_PENDING:
        return False, f"{REVIEW_PENDING}（快照/依赖未复核）"
    return True, ""


def build_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """发布快照：无环检查与历史复核都以固化的这组字段为准。"""
    return {
        "节点标识": node_key(entry),
        "节点编号": entry.get("节点编号"),
        "输入表名": entry.get("输入表名"),
        "输出表名": entry.get("输出表名"),
        "调度周期": entry.get("调度周期"),
        "执行顺序": entry.get("执行顺序"),
        "失败策略": entry.get("失败策略"),
        "依赖节点": entry.get("依赖节点"),
        "依赖节点引用": parse_dependency_tokens(entry.get("依赖节点")),
        "转换逻辑": entry.get("转换逻辑"),
    }


def snapshot_version(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"SNAP-{digest}"


def effective_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """已落库的发布快照优先；未固化的记录才按当前字段推导。"""
    stored = entry.get("发布快照")
    if isinstance(stored, dict) and stored:
        return dict(stored)
    return build_snapshot(entry)


def _int_order(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 执行计划 / 无环检查
# ---------------------------------------------------------------------------


def build_execution_plan(nodes: Sequence[dict[str, Any]], subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """基于发布快照构建执行计划，返回节点、边、环与计划问题。

    ``subject`` 用于“启用前预演”：把候选节点当作已启用加入图，其他仍只看已启用节点。
    """
    included: list[dict[str, Any]] = []
    for entry in nodes:
        if entry is subject:
            continue
        if entry.get("status") == "已启用":
            included.append(entry)
    if subject is not None:
        included.append(subject)

    plan_nodes: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for entry in included:
        snapshot = build_snapshot(entry) if entry is subject else effective_snapshot(entry)
        key = _clean_text(snapshot.get("节点标识") or node_key(entry))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        item = dict(snapshot)
        item["节点标识"] = key
        item["节点编号"] = item.get("节点编号") or key
        item["状态"] = entry.get("status")
        item["复核状态"] = entry.get("复核状态")
        plan_nodes.append(item)

    node_by_key = {_clean_text(item.get("节点标识")): item for item in plan_nodes}
    lookup = _node_lookup(included)
    edges: list[dict[str, str]] = []
    edge_set: set[tuple[str, str, str]] = set()
    issues: list[str] = []

    def add_edge(source: str, target: str, kind: str) -> None:
        if not source or not target:
            return
        triple = (source, target, kind)
        if triple in edge_set:
            return
        edge_set.add(triple)
        edges.append({"from": source, "to": target, "kind": kind})

    for item in plan_nodes:
        entry = next((row for row in included if node_key(row) == item["节点标识"]), None)
        if entry is None:
            continue
        for token in parse_dependency_tokens(item.get("依赖节点")):
            target = lookup.get(token) or lookup.get(token.upper())
            if target is None:
                continue
            target_key = node_key(target)
            if target_key not in node_by_key:
                continue
            add_edge(target_key, item["节点标识"], "dependency")

    # 表级连线：谁产出某张表，谁就是消费这张表的节点的上游。
    for consumer in plan_nodes:
        input_table = _clean_text(consumer.get("输入表名"))
        if not input_table:
            continue
        for producer in plan_nodes:
            if producer is consumer:
                continue
            if _clean_text(producer.get("输出表名")) == input_table:
                add_edge(producer["节点标识"], consumer["节点标识"], "table")

    # 自产自消也是环：同一节点输入表等于输出表。
    for item in plan_nodes:
        input_table = _clean_text(item.get("输入表名"))
        output_table = _clean_text(item.get("输出表名"))
        if input_table and input_table == output_table:
            add_edge(item["节点标识"], item["节点标识"], "table-self")
            issues.append(f"节点 {item['节点标识']} 的输入表与输出表相同")

    adjacency: dict[str, list[str]] = {key: [] for key in node_by_key}
    for edge in edges:
        adjacency.setdefault(edge["from"], []).append(edge["to"])
    for key in adjacency:
        adjacency[key] = sorted(set(adjacency[key]))

    # 颜色法找环，同时把环路径带出来，便于页面直接显示原因。
    colors: dict[str, int] = {key: 0 for key in adjacency}
    stack: list[str] = []
    cycle_path: list[str] = []

    def visit(key: str) -> bool:
        colors[key] = 1
        stack.append(key)
        for next_key in adjacency.get(key, []):
            if next_key not in colors:
                colors[next_key] = 0
                adjacency.setdefault(next_key, [])
            if colors[next_key] == 1:
                cycle_path.extend(stack[stack.index(next_key):] + [next_key])
                return True
            if colors[next_key] == 0 and visit(next_key):
                return True
        stack.pop()
        colors[key] = 2
        return False

    for key in list(adjacency):
        if colors.get(key, 0) == 0 and not cycle_path:
            visit(key)

    # 执行顺序 / 调度周期不是阻断条件，但会作为发布快照一致性提示返回。
    order_conflicts: list[str] = []
    schedule_conflicts: list[str] = []
    for edge in edges:
        if edge["kind"] != "dependency":
            continue
        source = node_by_key.get(edge["from"])
        target = node_by_key.get(edge["to"])
        if not source or not target:
            continue
        source_order = _int_order(source.get("执行顺序"))
        target_order = _int_order(target.get("执行顺序"))
        source_cycle = _clean_text(source.get("调度周期"))
        target_cycle = _clean_text(target.get("调度周期"))
        if source_order is not None and target_order is not None and source_cycle and source_cycle == target_cycle:
            if source_order >= target_order:
                order_conflicts.append(
                    f"依赖 {edge['from']} 的执行顺序 {source_order} 不早于 {edge['to']} 的 {target_order}"
                )
        if source_cycle and target_cycle and source_cycle != target_cycle:
            schedule_conflicts.append(
                f"依赖 {edge['from']} 的调度周期 {source_cycle} 与 {edge['to']} 的 {target_cycle} 不一致"
            )

    return {
        "nodes": plan_nodes,
        "edges": edges,
        "cycle": cycle_path,
        "valid": not cycle_path,
        "issues": issues,
        "order_conflicts": order_conflicts,
        "schedule_conflicts": schedule_conflicts,
    }


# ---------------------------------------------------------------------------
# 输入表生产者校验
# ---------------------------------------------------------------------------


def _active_table_names(table_rows: Iterable[dict[str, Any]], config: dict[str, Any]) -> tuple[set[str], set[str]]:
    active: set[str] = set()
    inactive: set[str] = set()
    for row in table_rows:
        names = {_clean_text(row.get(field)) for field in config["name_fields"] if _clean_text(row.get(field))}
        if not names:
            continue
        if _clean_text(row.get("status")) in config["active"]:
            active.update(names)
        else:
            inactive.update(names)
    return active, inactive


def validate_input_producer(entry: dict[str, Any], nodes: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    """输入表必须有一个落库且可用的生产者：已启用转换节点，或已上线的分层表。"""
    input_table = _clean_text(entry.get("输入表名"))
    output_table = _clean_text(entry.get("输出表名"))
    if not input_table:
        return False, "输入表名为空，无法确认生产者"
    if input_table == output_table:
        return False, f"输入表与输出表相同（{input_table}），节点会自产自消"
    if input_table == node_key(entry):
        return False, "输入表不能引用节点自身"

    enabled_producers = [
        node for node in nodes
        if node.get("status") == "已启用"
        and node.get("复核状态") != REVIEW_PENDING
        and _clean_text(node.get("输出表名")) == input_table
    ]
    if enabled_producers:
        return True, f"输入表 {input_table} 由已启用节点 {node_key(enabled_producers[0])} 产出"

    # 分层表目录里已上线的同名表本身就是合法生产者；必须先于“不可用节点”判断，
    # 否则一个尚未启用的同名节点会把已上线的表误判成没有生产者，并沿依赖链级联。
    for module, config in TABLE_MODULES.items():
        active, _inactive = _active_table_names(store.rows(module), config)
        if input_table in active:
            return True, f"输入表 {input_table} 已在 {module} 层登记且处于可用状态"

    # 到这里已没有可用生产者，再给出具体原因：优先指出停用/评审中的同名节点。
    unavailable_producers = [
        node for node in nodes
        if _clean_text(node.get("输出表名")) == input_table
        and node.get("status") != "已启用"
    ]
    if unavailable_producers:
        producer = unavailable_producers[0]
        return False, (
            f"输入表 {input_table} 的生产者节点 {node_key(producer)} "
            f"当前状态为{producer.get('status')}，不能作为合法生产者"
        )

    for module, config in TABLE_MODULES.items():
        _active, inactive = _active_table_names(store.rows(module), config)
        if input_table in inactive:
            return False, f"输入表 {input_table} 已登记但尚未上线，不能作为合法生产来源"
    return False, f"输入表 {input_table} 没有合法生产者（既无启用节点产出，也未在分层表登记上线）"


# ---------------------------------------------------------------------------
# 启用准入
# ---------------------------------------------------------------------------


def validate_transform_enable(
    entry: dict[str, Any], nodes: Sequence[dict[str, Any]]
) -> tuple[bool, list[str], dict[str, Any]]:
    """启用编排前的完整准入：字段、依赖存在且可用、输入表生产者、发布快照无环。"""
    reasons: list[str] = []
    details: dict[str, Any] = {}

    for field in ("节点编号", "输入表名", "输出表名"):
        if not _clean_text(entry.get(field)):
            reasons.append(f"缺少必填字段：{field}")
    if reasons:
        return False, reasons, details

    resolved, missing = resolve_dependencies(entry, nodes)
    details["依赖节点"] = [node_key(node) for node in resolved]
    details["缺失依赖"] = missing
    if missing:
        reasons.append(f"依赖节点不存在：{'、'.join(missing)}")
    inactive_deps = []
    for node in resolved:
        active, state = dependency_active(node)
        if not active:
            inactive_deps.append((node_key(node), node.get("status"), state))
    if inactive_deps:
        details["不可用依赖"] = inactive_deps
        reasons.append(
            "依赖节点不可用："
            + "、".join(f"{key}({status}-{state})" for key, status, state in inactive_deps)
        )

    producer_ok, producer_message = validate_input_producer(entry, nodes)
    details["输入表生产者"] = producer_message
    if not producer_ok:
        reasons.append(producer_message)

    # 只有前面这些条件都成立时，发布快照才具有可执行的图结构。
    if not reasons:
        plan = build_execution_plan(nodes, subject=entry)
        details["执行计划"] = plan
        details["发布快照"] = build_snapshot(entry)
        details["快照版本"] = snapshot_version(details["发布快照"])
        if plan["cycle"]:
            reasons.append(f"发布快照存在环：{' → '.join(plan['cycle'])}")
        if plan.get("issues"):
            reasons.extend(plan["issues"])

    return not reasons, reasons, details


# ---------------------------------------------------------------------------
# 节点依赖变化的影响范围
# ---------------------------------------------------------------------------


def affected_transform_scope(changed: dict[str, Any], nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """节点依赖变化后，返回自身 + 所有通过依赖/表流向下游的已启用节点。"""
    affected: list[dict[str, Any]] = [changed]
    queue: list[dict[str, Any]] = [changed]
    while queue:
        current = queue.pop(0)
        current_key = node_key(current)
        current_output = _clean_text(current.get("输出表名"))
        for candidate in nodes:
            if candidate in affected or candidate.get("status") != "已启用":
                continue
            refs = parse_dependency_tokens(candidate.get("依赖节点"))
            if current_key in refs or (current_output and _clean_text(candidate.get("输入表名")) == current_output):
                affected.append(candidate)
                queue.append(candidate)
    return affected


def mark_transform_review(
    entries: Sequence[dict[str, Any]],
    *,
    reason: str,
    operator: str | None = None,
    change_type: str = "准入复核",
) -> list[dict[str, Any]]:
    """把影响范围标记为待复核；只改复核状态，不重写已落库发布快照。"""
    changed: list[dict[str, Any]] = []
    for entry in entries:
        already_pending = entry.get("复核状态") == REVIEW_PENDING
        reasons = list(entry.get("复核原因") or [])
        if reason not in reasons:
            reasons.append(reason)
        entry["复核状态"] = REVIEW_PENDING
        entry["review_status"] = REVIEW_PENDING
        entry["复核原因"] = reasons
        entry["available"] = False
        entry["pending"] = True
        entry["abnormal"] = True
        if not already_pending:
            append_audit(
                table="transform",
                target=node_key(entry),
                change_type=change_type,
                before={"状态": entry.get("status"), "复核状态": REVIEW_PASSED},
                after={"状态": entry.get("status"), "复核状态": REVIEW_PENDING, "原因": reason},
                reason=reason,
                operator=operator,
            )
            changed.append(entry)
    return changed


def clear_transform_review(entry: dict[str, Any], *, reason: str, operator: str | None = None) -> None:
    """复核通过后清掉待复核标记；发布快照在调用方另行刷新。"""
    was_pending = entry.get("复核状态") == REVIEW_PENDING
    entry["复核状态"] = REVIEW_PASSED
    entry["review_status"] = REVIEW_PASSED
    entry["复核原因"] = []
    entry["available"] = entry.get("status") == "已启用"
    entry["pending"] = entry.get("status") != "已停用"
    entry["abnormal"] = entry.get("status") == "已停用"
    if was_pending:
        append_audit(
            table="transform",
            target=node_key(entry),
            change_type="复核完成",
            before={"复核状态": REVIEW_PENDING},
            after={"复核状态": REVIEW_PASSED, "状态": entry.get("status")},
            reason=reason,
            operator=operator,
        )


# ---------------------------------------------------------------------------
# 血缘版本仲裁
# ---------------------------------------------------------------------------


UPSTREAM_VERSION_KEYS = ("上游版本", "上游关系版本", "上游表版本", "upstream_version", "upstreamVersion", "source_version")
DOWNSTREAM_VERSION_KEYS = ("下游版本", "下游关系版本", "下游表版本", "downstream_version", "downstreamVersion", "target_version")
RELATION_VERSION_KEYS = ("关系版本", "血缘版本", "relation_version", "relationVersion", "版本")
UPSTREAM_FIELD_KEYS = ("上游字段", "upstream_field", "upstreamField", "源字段")
DOWNSTREAM_FIELD_KEYS = ("下游字段", "downstream_field", "downstreamField", "目标字段")
MAPPING_KEYS = ("字段映射", "上下游字段映射", "field_mapping", "fieldMapping")


def first_value(source: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and _clean_text(value) != "":
            return value
    return None


def normalise_mapping(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return [{"上游字段": _clean_text(k), "下游字段": _clean_text(v)} for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        result: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                result.append({
                    "上游字段": _clean_text(first_value(item, ("上游字段", "source", "from", "upstream_field"))),
                    "下游字段": _clean_text(first_value(item, ("下游字段", "target", "to", "downstream_field"))),
                })
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result.append({"上游字段": _clean_text(item[0]), "下游字段": _clean_text(item[1])})
            elif isinstance(item, str) and "→" in item:
                left, right = item.split("→", 1)
                result.append({"上游字段": left.strip(), "下游字段": right.strip()})
        return result or None
    text = _clean_text(value)
    if not text:
        return None
    if "→" in text:
        left, right = text.split("→", 1)
        return [{"上游字段": left.strip(), "下游字段": right.strip()}]
    return [{"上游字段": text, "下游字段": ""}]


def lineage_version_state(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "上游版本": _clean_text(first_value(entry, UPSTREAM_VERSION_KEYS)) or None,
        "下游版本": _clean_text(first_value(entry, DOWNSTREAM_VERSION_KEYS)) or None,
        "关系版本": _clean_text(first_value(entry, RELATION_VERSION_KEYS)) or None,
        "上游字段": _clean_text(first_value(entry, UPSTREAM_FIELD_KEYS)),
        "下游字段": _clean_text(first_value(entry, DOWNSTREAM_FIELD_KEYS)),
    }


def confirmation_binding(entry: dict[str, Any]) -> dict[str, Any] | None:
    binding = entry.get("确认快照")
    if isinstance(binding, dict) and binding:
        return binding
    return None


def version_drift(entry: dict[str, Any]) -> tuple[bool, str]:
    """已确认关系是否因为任一侧版本变化而需要复核。"""
    binding = confirmation_binding(entry)
    if not binding:
        return False, ""
    current_up = _clean_text(first_value(entry, UPSTREAM_VERSION_KEYS))
    current_down = _clean_text(first_value(entry, DOWNSTREAM_VERSION_KEYS))
    bound_up = _clean_text(binding.get("上游版本"))
    bound_down = _clean_text(binding.get("下游版本"))
    changed: list[str] = []
    if current_up != bound_up:
        changed.append(f"上游版本 {bound_up or '未绑定'} → {current_up or '未绑定'}")
    if current_down != bound_down:
        changed.append(f"下游版本 {bound_down or '未绑定'} → {current_down or '未绑定'}")
    if not changed:
        return False, ""
    return True, "；".join(changed)


def mark_lineage_review(
    entry: dict[str, Any],
    *,
    reason: str,
    operator: str | None = None,
    change_type: str = "版本漂移",
) -> bool:
    """把已确认关系回退到待复核；保留确认快照与历史，不覆盖旧字段关系。"""
    already = entry.get("status") == REVIEW_PENDING
    previous_status = entry.get("status")
    binding = confirmation_binding(entry)
    if binding:
        history = list(entry.get("确认历史") or [])
        if binding not in history:
            history.append(binding)
        entry["确认历史"] = history
    entry["status"] = REVIEW_PENDING
    entry["复核状态"] = REVIEW_PENDING
    entry["review_status"] = REVIEW_PENDING
    entry["available"] = False
    entry["pending"] = True
    entry["abnormal"] = True
    reasons = list(entry.get("复核原因") or [])
    if reason not in reasons:
        reasons.append(reason)
    entry["复核原因"] = reasons
    # 复核产生新的准入结论：旧解析结果不得继续用于确认，必须重新解析。
    entry["解析批次"] = ""
    entry["解析完成"] = False
    if not already:
        append_audit(
            table="lineage",
            target=_clean_text(entry.get("关系编号")) or str(entry.get("id", "")),
            change_type=change_type,
            before={"状态": previous_status, "确认快照": binding},
            after={"状态": REVIEW_PENDING, "原因": reason},
            reason=reason,
            operator=operator,
        )
        return True
    return False


def mark_lineage_result(
    entry: dict[str, Any],
    *,
    reason: str,
    operator: str | None = None,
) -> None:
    """解析/确认结论入账：保留旧确认，只更新复核结论与可用状态。"""
    binding = confirmation_binding(entry)
    entry["复核状态"] = REVIEW_PASSED
    entry["review_status"] = REVIEW_PASSED
    entry["复核原因"] = []
    entry["available"] = entry.get("status") == "已确认" and bool(binding)
    entry["pending"] = not entry["available"]
    entry["abnormal"] = False
    append_audit(
        table="lineage",
        target=_clean_text(entry.get("关系编号")) or str(entry.get("id", "")),
        change_type="复核完成",
        before={"状态": entry.get("status")},
        after={"状态": entry.get("status"), "复核状态": REVIEW_PASSED},
        reason=reason,
        operator=operator,
    )


# ---------------------------------------------------------------------------
# 跨业务面联动
# ---------------------------------------------------------------------------


def _table_pair(entry: dict[str, Any]) -> tuple[str, str]:
    return _clean_text(entry.get("上游表名")), _clean_text(entry.get("下游表名"))


def linked_lineage_entries(node: dict[str, Any]) -> list[dict[str, Any]]:
    """转换节点关联的血缘关系：直接表对 + 作为下游/上游的相邻关系。"""
    input_table = _clean_text(node.get("输入表名"))
    output_table = _clean_text(node.get("输出表名"))
    linked: list[dict[str, Any]] = []
    for relation in store.rows("lineage"):
        up, down = _table_pair(relation)
        relation_kind = ""
        if up == input_table and down == output_table:
            relation_kind = "直接上下游"
        elif down == input_table:
            relation_kind = "输入表上游"
        elif up == output_table:
            relation_kind = "输出表下游"
        if relation_kind:
            item = dict(relation)
            item["关联方式"] = relation_kind
            linked.append(item)
    return linked


def linked_transform_entries(relation: dict[str, Any]) -> list[dict[str, Any]]:
    """血缘关系关联的转换节点：消费上游表或产出下游表的节点。"""
    up, down = _table_pair(relation)
    linked: list[dict[str, Any]] = []
    for node in store.rows("transform"):
        input_table = _clean_text(node.get("输入表名"))
        output_table = _clean_text(node.get("输出表名"))
        relation_kind = ""
        if input_table == up and output_table == down:
            relation_kind = "直接转换节点"
        elif input_table == down:
            relation_kind = "下游消费节点"
        elif output_table == up:
            relation_kind = "上游生产节点"
        if relation_kind:
            item = dict(node)
            item["关联方式"] = relation_kind
            linked.append(item)
    return linked


def mark_linked_transform_review(
    reason: str, relation: dict[str, Any], operator: str | None = None
) -> list[dict[str, Any]]:
    """血缘版本漂移/废弃时，联动把相关已启用转换节点列入待复核，但不重写节点快照。"""
    targets = [
        node for node in linked_transform_entries(relation)
        if node.get("status") == "已启用" and node.get("复核状态") != REVIEW_PENDING
    ]
    return mark_transform_review(targets, reason=reason, operator=operator, change_type="跨面版本联动")


def mark_linked_lineage_review(
    reason: str, node: dict[str, Any], operator: str | None = None
) -> list[dict[str, Any]]:
    """节点依赖变化时，相关已确认血缘关系同步进入待复核，保留历史确认。"""
    changed: list[dict[str, Any]] = []
    for relation in linked_lineage_entries(node):
        stored = store.find("lineage", int(relation.get("id", 0)))
        if stored is None or stored.get("status") != "已确认":
            continue
        if mark_lineage_review(stored, reason=reason, operator=operator, change_type="跨面依赖联动"):
            changed.append(stored)
    return changed
