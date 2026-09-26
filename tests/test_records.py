import copy
import json
import unittest
from datetime import date
from pathlib import Path

from src.records import Book, load_records, parse_day
from src import configuration as cfgmod
from src import capacity as capmod
from src import delivery
from src import timeline

RECORDS_PATH = Path("fixtures/records.json")


def load():
    return load_records(RECORDS_PATH)


def raw():
    return json.loads(RECORDS_PATH.read_text(encoding="utf-8"))


def mutated(section, fn):
    """深拷贝样例，对某张表应用修改后重新装载。"""
    data = raw()
    fn(data[section])
    return Book(data)


class RecordsTest(unittest.TestCase):
    def test_fixture_loads_and_references_are_intact(self):
        book = load()
        self.assertEqual(book.data["domain"], "folding-house-export")
        self.assertGreaterEqual(book.data["version"], 2)

    def test_broken_reference_is_detected(self):
        data = raw()
        data["modules"][0]["supplier_id"] = "S-GHOST"
        with self.assertRaises(ValueError):
            load_records_from(data)


def load_records_from(data):
    book = Book(data)
    from src.records import validate_references
    errors = validate_references(book)
    if errors:
        raise ValueError("; ".join(errors))
    return book


class ConfigurationChangeTest(unittest.TestCase):
    def test_impact_lists_parts_price_weight_lead_time(self):
        book = load()
        ecn = next(e for e in book.changes if e["id"] == "ECN-2607-01")
        impact = cfgmod.change_impact(book, ecn)
        self.assertEqual(impact["added_modules"], ["GLASS-TRIPLE", "SMART-PV"])
        self.assertEqual(impact["removed_modules"], ["GLASS-DOUBLE"])
        self.assertEqual(impact["price_delta"], {"amount": 2200, "currency": "EUR"})
        self.assertEqual(impact["weight_delta_kg"], 180)
        self.assertEqual(impact["lead_time_delta_days"], 10)

    def test_all_declared_impacts_match_recomputation(self):
        book = load()
        for ecn in book.changes:
            self.assertTrue(
                cfgmod.impact_is_faithful(book, ecn),
                f"{ecn['id']} 声明的影响与复算不符",
            )

    def test_misreported_impact_is_rejected(self):
        data = raw()
        ecn = next(e for e in data["config_changes"] if e["id"] == "ECN-2607-01")
        ecn["impact"]["price_delta"]["amount"] = 999
        book = Book(data)
        self.assertFalse(cfgmod.impact_is_faithful(
            book, next(e for e in book.changes if e["id"] == "ECN-2607-01")))

    def test_change_before_production_needs_no_approval_and_is_allowed(self):
        book = load()
        ecn = next(e for e in book.changes if e["id"] == "ECN-2607-01")
        serials = ["SN-SYF20-001", "SN-SYF20-002"]
        # 决定日 2026-07-01 早于投产 2026-07-06
        ok, _ = cfgmod.change_is_allowed(book, ecn, serials)
        self.assertTrue(ok)

    def test_silent_switch_after_production_is_blocked(self):
        book = load()
        ecn = next(e for e in book.changes if e["id"] == "ECN-2607-02")
        ok, reason = cfgmod.change_is_allowed(
            book, ecn, ["SN-SYF20-001", "SN-SYF20-002"])
        self.assertFalse(ok)
        self.assertIn("投产", reason)

    def test_change_after_loading_is_always_blocked(self):
        book = load()
        ecn = next(e for e in book.changes if e["id"] == "ECN-2608-01")
        # SN-SYF20-001 于 2026-07-31 装柜，决定日 2026-08-02
        ok, reason = cfgmod.change_is_allowed(book, ecn, ["SN-SYF20-001"])
        self.assertFalse(ok)
        self.assertIn("装柜", reason)

    def test_recorded_change_states_match_rule_verdicts(self):
        book = load()
        for verdict in cfgmod.evaluate_changes(book):
            self.assertTrue(verdict["impact_faithful"], verdict)
            self.assertTrue(verdict["state_consistent"], verdict)

    def test_frozen_config_is_v2_for_built_serials(self):
        book = load()
        for serial_no in ("SN-SYF20-001", "SN-SYF20-002"):
            snap = cfgmod.signed_snapshot(book, serial_no)
            self.assertEqual(snap["version"], 2)
            self.assertEqual(snap["electrical"]["voltage"], "230V")
            self.assertIn("SMART-PV", snap["smart_modules"])
            self.assertEqual(snap["interior_package"], "comfort")

    def test_production_chain_rejects_silent_freeze(self):
        data = raw()
        prod = next(p for p in data["production"] if p["serial_no"] == "SN-SYF20-001")
        prod["frozen_config"] = {"config_id": "CFG-SYF20-EU-01", "version": 3}
        book = Book(data)
        results = {r["serial_no"]: r for r in timeline.production_config_chain_valid(book)}
        self.assertFalse(results["SN-SYF20-001"]["valid"])


class CapacityLockTest(unittest.TestCase):
    def test_first_claim_holds_confirmed_capacity(self):
        book = load()
        lk = next(l for l in book.locks if l["id"] == "LK-2606-01")
        ok, reason = capmod.evaluate_lock(book, lk)
        self.assertTrue(ok, reason)

    def test_contending_orders_split_by_confirmed_capacity(self):
        book = load()
        rows = {r["lock_id"]: r for r in capmod.evaluate_all_locks(book)}
        # 7 月已确认 2 套：第一单锁 2 套成立，第二单争 1 套被拒
        self.assertTrue(rows["LK-2606-01"]["status_consistent"])
        self.assertTrue(rows["LK-2607-01"]["status_consistent"])
        self.assertFalse(rows["LK-2607-01"]["feasible"])
        # 8 月产能确认后锁定成立
        self.assertTrue(rows["LK-2607-02"]["feasible"])
        self.assertTrue(rows["LK-2607-02"]["status_consistent"])

    def test_forecast_basis_cannot_lock(self):
        data = raw()
        lk = next(l for l in data["capacity_locks"] if l["id"] == "LK-2607-02")
        lk["basis"] = "forecast"
        book = Book(data)
        ok, _ = capmod.evaluate_lock(book, next(l for l in book.locks if l["id"] == "LK-2607-02"))
        self.assertFalse(ok)


class QuoteAndComplianceTest(unittest.TestCase):
    def test_order_signed_inside_quote_validity(self):
        book = load()
        order = book.orders["SO-2026-042"]
        self.assertTrue(delivery.order_signed_within_validity(book, order))
        self.assertTrue(delivery.signed_price_matches_quote(book, order))

    def test_expired_quote_cannot_anchor_order(self):
        book = load()
        ok, reason = delivery.quote_is_valid(book, "Q-2026-001", 1, date(2026, 7, 20))
        self.assertFalse(ok)
        self.assertIn("有效期", reason)

    def test_later_sales_revision_does_not_change_signed_order(self):
        book = load()
        order = book.orders["SO-2026-042"]
        rev2 = book.quote_revision("Q-2026-001", 2)
        self.assertEqual(rev2["lines"][0]["unit_price"]["amount"], 17900)
        # 已签订单仍锚定版本1的 18,500
        rev1 = book.quote_revision("Q-2026-001", 1)
        self.assertEqual(rev1["lines"][0]["unit_price"]["amount"], 18500)
        self.assertTrue(delivery.signed_price_matches_quote(book, order))

    def test_usd_quote_keeps_usd_currency_and_validity(self):
        book = load()
        ok, _ = delivery.quote_is_valid(book, "Q-2026-002", 1, date(2026, 7, 18))
        self.assertTrue(ok)
        order = book.orders["SO-2026-051"]
        self.assertTrue(delivery.signed_price_matches_quote(book, order))

    def test_certificate_covers_destination_and_station_access(self):
        book = load()
        for order in book.orders.values():
            for row in delivery.order_compliance(book, order):
                self.assertTrue(row["covers_country"], row)
                self.assertTrue(row["station_access"], row)

    def test_wrong_country_cert_is_detected(self):
        book = load()
        self.assertFalse(delivery.certificate_covers(
            book, "CERT-UL-US", "DE", date(2026, 7, 18)))
        self.assertFalse(delivery.station_can_access_cert(
            book, "ST-DE-HH", "CERT-UL-US"))


class QcAndShippingTest(unittest.TestCase):
    def test_failed_unit_is_not_released_until_reinspection(self):
        book = load()
        # 全部检验中最近的一条是站端复验合格，故现在可放行
        last = delivery.latest_inspection(book, "SN-SYF20-002")
        self.assertEqual(last["id"], "INS-005")
        self.assertEqual(last["result"], "pass")
        ok, _ = delivery.released_for_loading(book, "SN-SYF20-002")
        self.assertTrue(ok)

    def test_all_containers_hold_only_released_serials(self):
        book = load()
        rows = delivery.all_loaded_serials_released(book)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["released"], row["reason"])

    def test_unclosed_ncr_blocks_loading(self):
        data = raw()
        ncr = next(n for n in data["ncrs"] if n["id"] == "NCR-001")
        ncr["closed_on"] = None
        # 复验记录也撤掉，最近检验变为失败
        data["inspections"] = [i for i in data["inspections"] if i["id"] != "INS-003"]
        book = Book(data)
        ok, reason = delivery.released_for_loading(book, "SN-SYF20-002")
        self.assertFalse(ok)

    def test_split_shipments_preserve_quantities_and_no_cross_orders(self):
        book = load()
        plan = delivery.order_shipment_plan(book, "SO-2026-042")
        self.assertEqual(plan["batches"], ["B-2026-08-1", "B-2026-08-2"])
        self.assertEqual(plan["loaded_by_line"], {"L1": 2})
        self.assertTrue(plan["quantities_match"])
        self.assertEqual(plan["foreign_serials"], [])

    def test_cross_order_container_is_flagged(self):
        data = raw()
        # 把第二单的柜塞进第一单批次计划会产生串单（这里直接造外单柜）
        foreign = next(c for c in data["containers"] if c["id"] == "TCLU-440710")
        foreign["batch_id"] = "B-2026-08-1"
        book = Book(data)
        plan = delivery.order_shipment_plan(book, "SO-2026-042")
        self.assertIn("SN-TKC08-001", plan["foreign_serials"])


class DamagePermitOfflineTest(unittest.TestCase):
    def test_transit_damage_closed_loop_attributes_carrier(self):
        book = load()
        result = delivery.damage_resolution(book, "DMG-001")
        self.assertTrue(result["seals_intact_on_arrival"])
        self.assertTrue(result["joint_survey_held"])
        self.assertEqual(result["responsibility"], "carrier")
        self.assertTrue(result["claim_opened"])
        self.assertTrue(result["repair_verified"])
        self.assertTrue(result["closed_loop"])

    def test_damage_without_claim_is_not_closed_loop(self):
        data = raw()
        dmg = next(d for d in data["damage_reports"] if d["id"] == "DMG-001")
        dmg["claim_no"] = None
        book = Book(data)
        self.assertFalse(delivery.damage_resolution(book, "DMG-001")["closed_loop"])

    def test_delayed_electrical_permit_blocks_energization_until_granted(self):
        book = load()
        granted, pending = delivery.permits_granted(book, "DE-SITE-7", date(2026, 9, 12))
        self.assertFalse(granted)
        self.assertIn("electrical", pending)
        granted, _ = delivery.permits_granted(book, "DE-SITE-7", date(2026, 9, 19))
        self.assertTrue(granted)

    def test_offline_records_dedup_and_quarantine_conflicts(self):
        book = load()
        rec = delivery.reconcile_offline_records(book)
        # off-7f31-a1 两条完全相同 -> 折叠为 1 条
        dup_ids = {d["client_event_id"] for d in rec["duplicates_collapsed"]}
        self.assertIn("off-7f31-a1", dup_ids)
        # off-9c02-b7 同号不同 hash -> 隔离
        conflict_ids = {c["client_event_id"] for c in rec["conflicts_quarantined"]}
        self.assertIn("off-9c02-b7", conflict_ids)
        self.assertTrue(rec["energization_after_permits"])

    def test_energization_before_permit_is_violation(self):
        data = raw()
        inst = next(i for i in data["installations"] if i["serial_no"] == "SN-SYF20-001")
        inst["energized_on"] = "2026-09-10"  # 电气许可 9-18 才批
        book = Book(data)
        self.assertFalse(delivery.reconcile_offline_records(book)["energization_after_permits"])


class AcceptanceTest(unittest.TestCase):
    def test_itemwise_match_against_signed_version_can_sign(self):
        book = load()
        result = delivery.evaluate_acceptance(book, "ACC-001")
        self.assertEqual(result["mismatches"], [])
        self.assertTrue(result["may_sign"])
        self.assertTrue(result["signed_valid"])
        # 核对基准必须是冻结的签署版本 v2，而非后来的报价版本
        self.assertEqual(result["against"], {"config_id": "CFG-SYF20-EU-01", "version": 2})

    def test_mismatch_blocks_signature_until_punch_closed(self):
        book = load()
        first = delivery.evaluate_acceptance(book, "ACC-002")
        self.assertTrue(first["mismatches"])
        self.assertFalse(first["may_sign"])
        # 记录如实标记未签署，因此该记录本身一致（违规签署见下一条）
        self.assertTrue(first["signed_valid"])
        raw_acc = next(a for a in book.data["acceptance_checks"] if a["id"] == "ACC-002")
        self.assertFalse(raw_acc["signed_by_customer"])
        self.assertIsNone(raw_acc["signed_on"])
        second = delivery.evaluate_acceptance(book, "ACC-003")
        self.assertTrue(second["may_sign"])
        self.assertTrue(second["signed_valid"])

    def test_signed_with_open_punch_is_invalid(self):
        data = raw()
        acc = next(a for a in data["acceptance_checks"] if a["id"] == "ACC-002")
        acc["signed_by_customer"] = True
        acc["signed_on"] = "2026-09-20"
        book = Book(data)
        self.assertFalse(delivery.evaluate_acceptance(book, "ACC-002")["signed_valid"])


class SerialDossierTest(unittest.TestCase):
    def test_dossier_reproduces_decisions_with_responsibility(self):
        book = load()
        dossier = timeline.serial_dossier(book, "SN-SYF20-002")
        self.assertEqual(dossier["as_built"]["electrical"]["voltage"], "230V")
        self.assertEqual(dossier["destination_country"], "DE")
        self.assertTrue(dossier["order"]["frozen_chain_valid"])
        self.assertTrue(dossier["certificate"]["accessible_at_this_station"])

        stages = [d["stage"] for d in dossier["decisions"]]
        for stage in ("production", "qc", "packing", "shipping",
                      "acceptance", "warranty"):
            self.assertIn(stage, stages)

        # 运输损伤的责任归属可在档案中重现
        damage = dossier["damages"][0]
        self.assertEqual(damage["determined_responsibility"], "carrier")
        ncr_resp = {n["id"]: n["responsibility"] for n in dossier["ncrs"]}
        self.assertEqual(ncr_resp["NCR-001"], "factory")
        self.assertEqual(ncr_resp["NCR-002"], "carrier")

        # 保修已起算
        self.assertEqual(dossier["warranty"]["months"], 24)

    def test_dossier_includes_capsule_station_stock_case(self):
        book = load()
        dossier = timeline.serial_dossier(book, "SN-TKC08-001")
        self.assertEqual(dossier["as_built"]["electrical"]["frequency"], "60Hz")
        self.assertIn("SMART-PV", dossier["as_built"]["smart_modules"])
        self.assertEqual(dossier["certificate"]["id"], "CERT-UL-US")
        self.assertEqual(dossier["shipment"]["status"], "in-station-stock")

    def test_station_scan_shows_as_built_not_last_sales_quote(self):
        book = load()
        snap = cfgmod.signed_snapshot(book, "SN-SYF20-001")
        # 销售后来把报价改成 17,900（r2），扫码仍显示制造时 v2
        self.assertEqual(snap["config_id"], "CFG-SYF20-EU-01")
        self.assertEqual(snap["version"], 2)
        cfg = book.config("CFG-SYF20-EU-01", 2)
        self.assertEqual(cfg["unit_price"], {"amount": 20700, "currency": "EUR"})

    def test_every_production_serial_has_valid_config_chain(self):
        book = load()
        for row in timeline.production_config_chain_valid(book):
            self.assertTrue(row["valid"], row)


if __name__ == "__main__":
    unittest.main()
