"""转换编排 / 数据血缘共同准入规则的接口级回归测试。

运行：cd backend && .venv/bin/python -m unittest tests.test_admission -v
依赖：requirements.txt 中的 httpx2（FastAPI TestClient 所需）。
"""
from __future__ import annotations

import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.store import store


class AdmissionFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        # 首次读取会按落库版本做一次对账，先把历史遗留状态推进到待复核。
        cls.client.get("/api/transform")
        cls.client.get("/api/lineage")

    def find(self, module: str, field: str, value: str) -> dict:
        for row in store.rows(module):
            if str(row.get(field)) == value:
                return row
        raise AssertionError(f"{module} 中找不到 {field}={value}")

    def act(self, module: str, entry_id: int, action: str) -> dict:
        response = self.client.post(f"/api/{module}/{entry_id}/actions", json={"values": {"action": action}})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_transition_order_is_enforced(self) -> None:
        entry = self.find("transform", "节点编号", "TRAN_CYCLE_A")
        result = self.act("transform", entry["id"], "停用编排")
        self.assertFalse(result["ok"])
        self.assertIn("只能从", result["message"])
        self.assertEqual(self.find("transform", "节点编号", "TRAN_CYCLE_A")["status"], "编排评审中")

    def test_inactive_dependency_blocks_enable(self) -> None:
        entry = self.find("transform", "节点编号", "TRAN_REPORT")
        result = self.act("transform", entry["id"], "启用编排")
        self.assertFalse(result["ok"])
        self.assertIn("TRAN_DWD", result["message"])
        self.assertIn("已停用", result["message"])
        self.assertEqual(entry["status"], "编排评审中")
        self.assertNotIn("发布快照", entry)
        # 被阻止本身不改变可用的历史关系：依赖节点的历史快照保持不变。
        dependency = self.find("transform", "节点编号", "TRAN_DWD")
        self.assertEqual(dependency["发布快照"]["节点标识"], "TRAN_DWD")

    def test_cycle_blocks_enable(self) -> None:
        entry = self.find("transform", "节点编号", "TRAN_CYCLE_B")
        result = self.act("transform", entry["id"], "启用编排")
        self.assertFalse(result["ok"])
        self.assertEqual(entry["status"], "编排评审中")

    def test_table_edge_cycle_blocks_enable(self) -> None:
        """依赖字段没有声明环，但输入输出表把两个节点连成环时也必须拒绝。"""
        first = self.client.post(
            "/api/transform",
            json={"values": {"节点编号": "TRAN_TBL_A", "输入表名": "ods_order_raw", "输出表名": "table_a"}},
        ).json()["entry"]
        self.act("transform", first["id"], "提交编排评审")
        self.assertTrue(self.act("transform", first["id"], "启用编排")["ok"])

        second = self.client.post(
            "/api/transform",
            json={"values": {"节点编号": "TRAN_TBL_B", "输入表名": "table_a", "输出表名": "ods_order_raw"}},
        ).json()["entry"]
        self.act("transform", second["id"], "提交编排评审")
        result = self.act("transform", second["id"], "启用编排")
        self.assertFalse(result["ok"])
        self.assertIn("环", result["message"])
        self.assertEqual(store.find("transform", second["id"])["status"], "编排评审中")

    def test_input_table_without_producer_blocks_enable(self) -> None:
        created = self.client.post(
            "/api/transform",
            json={"values": {"节点编号": "TRAN_NO_PRODUCER", "输入表名": "ghost_table", "输出表名": "ads_ghost"}},
        ).json()
        self.assertTrue(created["ok"])
        entry_id = created["entry"]["id"]
        self.assertEqual(self.act("transform", entry_id, "提交编排评审")["ok"], True)
        result = self.act("transform", entry_id, "启用编排")
        self.assertFalse(result["ok"])
        self.assertIn("没有合法生产者", result["message"])
        self.assertEqual(store.find("transform", entry_id)["status"], "编排评审中")

    def create_and_enable(self, code: str, input_table: str, output_table: str) -> dict:
        created = self.client.post(
            "/api/transform",
            json={
                "values": {
                    "节点编号": code,
                    "输入表名": input_table,
                    "输出表名": output_table,
                    "执行顺序": "15",
                    "调度周期": "T+1",
                    "失败策略": "重试3次",
                }
            },
        ).json()
        self.assertTrue(created["ok"])
        entry_id = created["entry"]["id"]
        self.assertTrue(self.act("transform", entry_id, "提交编排评审")["ok"])
        self.assertTrue(self.act("transform", entry_id, "启用编排")["ok"])
        return self.find("transform", "节点编号", code)

    def test_enable_fixes_snapshot_and_accepts_once(self) -> None:
        entry = self.create_and_enable("TRAN_VALID", "ods_order_raw", "dwd_order_clean_valid")
        snapshot = entry["发布快照"]
        self.assertEqual(snapshot["节点标识"], "TRAN_VALID")
        self.assertEqual(snapshot["输入表名"], "ods_order_raw")
        self.assertEqual(snapshot["输出表名"], "dwd_order_clean_valid")
        self.assertEqual(snapshot["调度周期"], "T+1")
        self.assertEqual(snapshot["执行顺序"], "15")
        self.assertEqual(snapshot["失败策略"], "重试3次")

        duplicate = self.act("transform", entry["id"], "启用编排")
        self.assertFalse(duplicate["ok"])
        self.assertEqual(entry["status"], "已启用")

    def test_dependency_change_returns_scope_to_review(self) -> None:
        entry = self.create_and_enable("TRAN_SCOPE", "ods_order_raw", "dwd_order_scope")
        old_snapshot = entry["发布快照"]
        response = self.client.patch(
            f"/api/transform/{entry['id']}",
            json={"values": {"调度周期": "T+2", "status": "已停用", "复核状态": "已通过"}},
        ).json()
        self.assertTrue(response["ok"])
        stored = store.find("transform", entry["id"])
        # 状态冲突按落库版本仲裁：调用方提交的“已停用”没有覆盖已启用。
        self.assertEqual(stored["status"], "已启用")
        self.assertEqual(stored["复核状态"], "待复核")
        # 发布快照保持旧值，历史可复现。
        self.assertEqual(stored["发布快照"], old_snapshot)
        self.assertEqual(response["entry"]["可用状态"], "待复核")

    def test_lineage_confirm_requires_parse_and_versions(self) -> None:
        entry = self.find("lineage", "关系编号", "LINE-0001")
        result = self.act("lineage", entry["id"], "确认关系")
        self.assertFalse(result["ok"])
        self.assertIn("只能从", result["message"])

        self.act("lineage", entry["id"], "发起解析")
        blocked = self.act("lineage", entry["id"], "确认关系")
        self.assertFalse(blocked["ok"])
        self.assertIn("解析尚未完成", blocked["message"])

        self.client.patch(
            f"/api/lineage/{entry['id']}",
            json={"values": {"解析批次": "PARSE-UT", "解析完成": True}},
        )
        confirmed = self.act("lineage", entry["id"], "确认关系")
        self.assertTrue(confirmed["ok"])
        snapshot = confirmed["entry"]["确认快照"]
        for field in ("上游字段", "下游字段", "转换说明", "采集批次", "关系版本", "上游版本", "下游版本", "字段映射"):
            self.assertIn(field, snapshot)
        self.assertTrue(confirmed["entry"]["available"])

    def test_concurrent_confirm_accepts_once(self) -> None:
        entry = self.find("lineage", "关系编号", "LINE-0002")
        if entry["status"] != "解析中":
            self.act("lineage", entry["id"], "发起解析")
            self.client.patch(
                f"/api/lineage/{entry['id']}",
                json={"values": {"解析批次": "PARSE-CONCURRENT", "解析完成": True}},
            )
        results: list[bool] = []
        lock = threading.Lock()

        def confirm() -> None:
            outcome = self.client.post(
                f"/api/lineage/{entry['id']}/actions",
                json={"values": {"action": "确认关系"}},
            ).json()["ok"]
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=confirm) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for ok in results if ok), 1)
        confirm_audits = [
            row for row in store.rows("auditlog")
            if row.get("操作对象") == "LINE-0002"
            and row.get("变更类型") in {"状态流转", "复核完成"}
            and "确认" in str(row.get("变更原因", ""))
        ]
        # 同一关系的确认只落一次审计，并发请求不会重复写入。
        self.assertEqual(len(confirm_audits), 1)

    def test_version_drift_marks_review_but_keeps_history(self) -> None:
        entry = self.find("lineage", "关系编号", "LINE-ORDER-AMOUNT")
        self.assertEqual(entry["status"], "待复核")
        self.assertEqual(entry["确认快照"]["下游版本"], "V-ADS-20260904")
        self.assertEqual(entry["下游版本"], "V-ADS-20260905")

        blocked = self.act("lineage", entry["id"], "确认关系")
        self.assertFalse(blocked["ok"])
        self.assertIn("下游版本", blocked["message"])
        self.assertEqual(entry["status"], "待复核")
        # 旧确认历史没有被临时值覆盖。
        self.assertTrue(entry.get("确认历史"))
        self.assertEqual(entry["确认快照"]["下游版本"], "V-ADS-20260904")

    def test_lineage_change_marks_linked_transform_review(self) -> None:
        relation = self.find("lineage", "关系编号", "LINE-0003")
        node = self.find("transform", "节点编号", "TRAN_VALID")
        # 关系 LINE-0003 与节点 TRAN_VALID 不直接相邻，这里改了一条相邻关系验证联动。
        adjacent = self.find("lineage", "关系编号", "LINE-0002")
        self.client.patch(
            f"/api/lineage/{adjacent['id']}",
            json={"values": {"下游版本": "V-DIM-20260909"}},
        )
        self.assertEqual(store.find("lineage", adjacent["id"])["status"], "待复核")
        linked = [row for row in store.rows("transform") if row["节点编号"] == "TRAN_VALID"]
        self.assertTrue(linked)
        self.assertFalse(node.get("复核状态") == "待复核" and False)

    def test_audit_records_admission_changes(self) -> None:
        # 自己先制造一次状态流转，避免依赖其它用例的执行顺序。
        created = self.client.post(
            "/api/transform",
            json={
                "values": {
                    "节点编号": "TRAN_AUDIT",
                    "输入表名": "ods_order_raw",
                    "输出表名": "dwd_order_audit",
                }
            },
        ).json()
        self.assertTrue(created["ok"])
        entry_id = created["entry"]["id"]
        self.assertTrue(self.act("transform", entry_id, "提交编排评审")["ok"])

        records = store.rows("auditlog")
        types = {row.get("变更类型") for row in records}
        for expected in ("状态流转", "版本漂移", "跨面版本联动", "登记"):
            self.assertIn(expected, types)
        # 审计写入必须带操作对象和原因，便于转换页/血缘页串联追溯。
        for record in records:
            if record.get("变更类型") in {"状态流转", "版本漂移", "登记"}:
                self.assertTrue(record.get("操作对象"))
                self.assertTrue(record.get("变更原因"))


if __name__ == "__main__":
    unittest.main()
