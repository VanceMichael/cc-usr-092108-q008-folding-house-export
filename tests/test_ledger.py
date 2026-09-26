import copy
import json
import unittest
from pathlib import Path

from src.ledger import LedgerError, load_ledger, serial_trace, validate_ledger

FIXTURE = Path("fixtures/ledger.json")


class LedgerFixtureTest(unittest.TestCase):
    def setUp(self):
        self.ledger = load_ledger(FIXTURE)

    def test_fixture_valid(self):
        self.assertEqual(self.ledger["domain"], "folding-house-export")
        self.assertEqual(len(self.ledger["orders"]), 2)

    def test_capacity_contention_split(self):
        # Q2 二代中控槽位 6 套：ORD-2601 锁 4、ORD-2607 锁 2，恰好不超卖
        order_2607 = next(o for o in self.ledger["orders"] if o["order_id"] == "ORD-2607")
        q2 = [lk for lk in order_2607["capacity_locks"] if lk["slot_id"] == "SLOT-V2-Q2"]
        q3 = [lk for lk in order_2607["capacity_locks"] if lk["slot_id"] == "SLOT-V2-Q3"]
        self.assertEqual(sum(lk["qty"] for lk in q2), 2)
        self.assertEqual(sum(lk["qty"] for lk in q3), 1)
        self.assertEqual(len(order_2607["shipments"]), 2)

    def test_serial_trace_reproduces_decisions(self):
        trace = serial_trace(self.ledger, "FH-DPX-0003")
        stages = [entry["stage"] for entry in trace]
        self.assertEqual(stages[0], "configuration")
        self.assertIn("230V/50Hz", trace[0]["summary"])
        self.assertIn("200kph", trace[0]["summary"])
        self.assertIn("qc", stages)
        self.assertIn("loading", stages)
        self.assertIn("shipping", stages)
        self.assertIn("acceptance", stages)
        self.assertIn("warranty", stages)
        # 每个事件都能看到决定人，支持责任归属重现
        for entry in trace[1:]:
            self.assertTrue(entry["decided_by"])
        # 事件按时间排序，且无重复
        times = [entry["at"] for entry in trace[1:]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), len(set((e["at"], e["type"]) for e in trace[1:])))


def _raw_ledger():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _order(ledger, order_id):
    return next(o for o in ledger["orders"] if o["order_id"] == order_id)


class InvariantTest(unittest.TestCase):
    def assert_rejected(self, mutate, message_contains):
        ledger = _raw_ledger()
        mutate(ledger)
        with self.assertRaisesRegex(LedgerError, message_contains):
            validate_ledger(ledger)

    # 结构合约 ---------------------------------------------------------------
    def test_structure_missing_field_rejected(self):
        def m(ledger):
            del _order(ledger, "ORD-2601")["quotation"]["accepted"]
        self.assert_rejected(m, "结构不符合合约")

    # 不变量 1：汇总与明细一致 ------------------------------------------------
    def test_weight_total_mismatch_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["configuration_revisions"][1]["totals"]["weight_kg"] = 9999
        self.assert_rejected(m, "汇总重量")

    def test_price_total_mismatch_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["configuration_revisions"][1]["totals"]["price"][0]["amount"] = 1
        self.assert_rejected(m, "汇总价")

    # 不变量 2：变更四要素 ----------------------------------------------------
    def test_change_order_without_price_impact_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["change_orders"][0]["price_delta"] = []
        self.assert_rejected(m, "价格影响")

    def test_change_order_wrong_weight_delta_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["change_orders"][0]["weight_delta_kg"] = 100
        self.assert_rejected(m, "重量影响")

    def test_change_order_wrong_delivery_delta_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["change_orders"][0]["delivery_delta_days"] = 99
        self.assert_rejected(m, "交期影响")

    # 不变量 3/4：投产与装柜冻结 ---------------------------------------------
    def test_batch_quantity_silently_changed_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["batches"][0]["quantity"] = 5
        self.assert_rejected(m, "投产数量不得静默变化")

    def test_batch_on_superseded_revision_rejected(self):
        def m(ledger):
            batch = _order(ledger, "ORD-2601")["batches"][0]
            batch["revision"] = 1
            batch["release_event_id"]  # 引用不变，先触发版本冻结检查
        self.assert_rejected(m, "禁止静默切换版本")

    def test_change_after_production_keeps_as_built_batch(self):
        # 显式换版在投产之后批准：已投产批次按 as-built 旧版合法保留，验收按旧版核对
        ledger = _raw_ledger()
        order = _order(ledger, "ORD-2601")
        new_time = "2026-03-10T11:30:00Z"
        order["change_orders"][0]["approved_at"] = new_time
        order["configuration_revisions"][1]["signed_at"] = new_time
        order["batches"][0]["revision"] = 1
        order["containers"][0]["revision"] = 1
        rev1_options = sorted(
            (o["module_id"], o["option_id"])
            for o in order["configuration_revisions"][0]["options"]
        )
        for item in order["acceptance"]["items"]:
            item["revision"] = 1
            item["checks"] = [
                {"module_id": module_id, "option_id": option_id, "result": "matched"}
                for module_id, option_id in rev1_options
            ]
        validate_ledger(ledger)  # 不抛异常即通过

    def test_container_revision_swap_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["containers"][0]["revision"] = 1
        self.assert_rejected(m, "版本与箱封版本不一致")

    def test_under_production_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["quantity"] = 5
        self.assert_rejected(m, "投产总数少于订单数量")

    # 不变量 5：产能不超卖 ----------------------------------------------------
    def test_capacity_oversell_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2607")["capacity_locks"].append({
                "lock_id": "LOCK-BAD", "slot_id": "SLOT-V2-Q2",
                "module_id": "M-SMART", "option_id": "S-V2",
                "qty": 1, "status": "active", "created_at": "2026-02-09T11:00:00Z"
            })
        self.assert_rejected(m, "产能超卖")

    def test_released_lock_does_not_consume_capacity(self):
        ledger = _raw_ledger()
        locks = _order(ledger, "ORD-2601")["capacity_locks"]
        locks.append({
            "lock_id": "LOCK-EXTRA-RELEASED", "slot_id": "SLOT-V2-Q2",
            "module_id": "M-SMART", "option_id": "S-V2",
            "qty": 10, "status": "released", "created_at": "2026-02-01T00:00:00Z"
        })
        validate_ledger(ledger)  # 已释放不占用，不构成超卖

    # 不变量 6：报价有效期 ----------------------------------------------------
    def test_acceptance_of_expired_quotation_rejected(self):
        def m(ledger):
            quotation = _order(ledger, "ORD-2601")["quotation"]
            quotation["accepted"] = {"currency": "GBP", "accepted_at": "2026-01-20T14:05:00Z"}
        self.assert_rejected(m, "报价已于")

    # 不变量 7：证书评审与访问留痕 -------------------------------------------
    def test_pending_certificate_blocks_production(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["certificates"][0]["status"] = "pending"
        self.assert_rejected(m, "未通过评审不得投产")

    def test_certificate_without_access_grant_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["certificates"][1]["access_grants"] = []
        self.assert_rejected(m, "访问授权留痕|access_grants")

    def test_production_before_certificate_review_rejected(self):
        def m(ledger):
            # 证书评审时间被推迟到第一批投产之后（事件链不动）
            for cert in _order(ledger, "ORD-2607")["certificates"]:
                cert["reviewed_at"] = "2026-04-01T00:00:00Z"
        self.assert_rejected(m, "早于证书评审完成时间")

    # 不变量 8：未闭环不合格项不得装柜 ---------------------------------------
    def test_open_qc_failure_blocks_loading(self):
        def m(ledger):
            unit = _order(ledger, "ORD-2601")["batches"][0]["units"][1]
            unit["qc_items"][3]["closed"] = False  # 0002 的内装让步项
        self.assert_rejected(m, "未闭环质检不合格项，不得装柜")

    # 不变量 9：断网记录必须回传勾对 -----------------------------------------
    def test_unreconciled_offline_blocks_acceptance(self):
        def m(ledger):
            offline = _order(ledger, "ORD-2601")["offline_installs"][0]
            offline["synced_at"] = None
            offline["reconciled_at"] = None
            offline["sync_event_id"] = None
        self.assert_rejected(m, "尚未回传勾对")

    # 不变量 10：验收逐项与签署版本一致 --------------------------------------
    def test_acceptance_defect_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["acceptance"]["items"][0]["checks"][0]["result"] = "defect"
        self.assert_rejected(m, "验收项")

    def test_acceptance_wrong_option_rejected(self):
        def m(ledger):
            checks = _order(ledger, "ORD-2601")["acceptance"]["items"][0]["checks"]
            checks[0]["option_id"] = "E-US"  # 实物与签署版本不符
        self.assert_rejected(m, "验收项.*签署版本选项不一致")

    def test_acceptance_missing_serial_rejected(self):
        def m(ledger):
            items = _order(ledger, "ORD-2601")["acceptance"]["items"]
            items.pop()  # 漏验一座
        self.assert_rejected(m, "验收序列号集合")

    # 事件链 -----------------------------------------------------------------
    def test_unsorted_events_rejected(self):
        def m(ledger):
            event = next(e for e in _order(ledger, "ORD-2601")["events"]
                         if e["event_id"] == "ev-2601-load")
            event["at"] = "2026-02-01T00:00:00Z"
        self.assert_rejected(m, "事件链未按时间排序")

    def test_dangling_event_reference_rejected(self):
        def m(ledger):
            _order(ledger, "ORD-2601")["batches"][0]["release_event_id"] = "ev-does-not-exist"
        self.assert_rejected(m, "引用的事件")

    # 不变量 12：序列号全局唯一 ----------------------------------------------
    def test_serial_reuse_across_orders_rejected(self):
        def m(ledger):
            order = _order(ledger, "ORD-2607")
            order["batches"][1]["units"][0]["serial_number"] = "FH-DPX-0001"
            order["shipments"][1]["serials"] = ["FH-DPX-0001"]
            order["containers"][1]["serials"] = ["FH-DPX-0001"]
            order["acceptance"]["items"][2]["serial_number"] = "FH-DPX-0001"
        self.assert_rejected(m, "被两座房屋重复使用")

    def test_trace_unknown_serial_rejected(self):
        with self.assertRaisesRegex(LedgerError, "序列号 FH-NOPE-9999 不存在"):
            serial_trace(_raw_ledger(), "FH-NOPE-9999")

    # 异常单 -----------------------------------------------------------------
    def test_closed_exception_requires_close_record(self):
        def m(ledger):
            exc = _order(ledger, "ORD-2601")["exceptions"][0]
            exc["closed_event_id"] = None
        self.assert_rejected(m, "标记闭环但缺少闭环记录")


if __name__ == "__main__":
    unittest.main()
