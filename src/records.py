"""装载全链路记录并校验参照完整性。"""

import json
from datetime import datetime, date
from pathlib import Path


REQUIRED_TABLES = [
    "products", "modules", "suppliers", "service_stations", "certificates",
    "configuration_versions", "quotes", "inquiries", "orders", "config_changes",
    "capacity_locks", "production", "inspections", "ncrs", "containers",
    "shipments", "damage_reports", "permits", "offline_install_records",
    "installations", "acceptance_checks", "punch_items", "warranties", "events",
]


def parse_day(value):
    """接受 YYYY-MM-DD 或完整时间戳，返回 date。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value)
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    return date.fromisoformat(text)


class Book:
    """对记录建立索引，供各领域规则查询。"""

    def __init__(self, data: dict):
        self.data = data
        missing = [t for t in REQUIRED_TABLES if t not in data]
        if missing:
            raise ValueError(f"记录缺少必要表: {missing}")
        self._index()

    def _index(self):
        d = self.data
        self.products = {p["id"]: p for p in d["products"]}
        self.modules = {m["id"]: m for m in d["modules"]}
        self.suppliers = {s["id"]: s for s in d["suppliers"]}
        self.stations = {s["id"]: s for s in d["service_stations"]}
        self.certificates = {c["id"]: c for c in d["certificates"]}
        self.configs = {}
        for c in d["configuration_versions"]:
            self.configs[(c["config_id"], c["version"])] = c
        self.inquiries = {i["id"]: i for i in d["inquiries"]}
        self.orders = {o["id"]: o for o in d["orders"]}
        self.serials = {p["serial_no"]: p for p in d["production"]}
        self.locks = d["capacity_locks"]
        self.changes = d["config_changes"]
        self.quotes = {q["id"]: q for q in d["quotes"]}
        self.ncrs = {n["id"]: n for n in d["ncrs"]}
        self.punches = {p["id"]: p for p in d["punch_items"]}
        self.damages = {m["id"]: m for m in d["damage_reports"]}
        self.permits = {p["id"]: p for p in d["permits"]}

    # -- 便捷查询 -------------------------------------------------
    def config(self, config_id: str, version: int) -> dict:
        return self.configs[(config_id, version)]

    def order_line(self, order_id: str, line_id: str) -> dict:
        for line in self.orders[order_id]["lines"]:
            if line["line_id"] == line_id:
                return line
        raise KeyError(f"订单 {order_id} 无行 {line_id}")

    def line_serials(self, order_id: str, line_id: str) -> list[dict]:
        return [p for p in self.data["production"]
                if p["order_id"] == order_id and p["line_id"] == line_id]

    def serial_container(self, serial_no: str) -> dict | None:
        for c in self.data["containers"]:
            if serial_no in c["serials"]:
                return c
        return None

    def serial_shipment(self, serial_no: str) -> dict | None:
        container = self.serial_container(serial_no)
        if not container:
            return None
        for s in self.data["shipments"]:
            if container["id"] in s["containers"]:
                return s
        return None

    def quote_revision(self, quote_id: str, revision: int) -> dict:
        for r in self.quotes[quote_id]["revisions"]:
            if r["revision"] == revision:
                return r
        raise KeyError(f"报价 {quote_id} 无版本 {revision}")


def load_records(path: Path) -> Book:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    book = Book(data)
    errors = validate_references(book)
    if errors:
        raise ValueError("参照完整性错误: " + "; ".join(errors))
    return book


def validate_references(book: Book) -> list[str]:
    """返回全部断裂引用；空列表表示完整。"""
    errors: list[str] = []
    d = book.data

    for m in d["modules"]:
        if m["supplier_id"] not in book.suppliers:
            errors.append(f"模块 {m['id']} 的供应商 {m['supplier_id']} 不存在")

    for s in d["service_stations"]:
        for cert_id in s["cert_access"]:
            if cert_id not in book.certificates:
                errors.append(f"服务站 {s['id']} 被授权的证书 {cert_id} 不存在")

    for cap in d["suppliers"]:
        for w in cap["confirmed_capacity"]:
            if w["module_id"] not in book.modules:
                errors.append(f"供应商 {cap['id']} 产能引用未知模块 {w['module_id']}")

    for c in d["configuration_versions"]:
        if c["model_id"] not in book.products:
            errors.append(f"配置 {c['config_id']} v{c['version']} 引用未知户型")
        for mid in c["modules"]:
            if mid not in book.modules:
                errors.append(f"配置 {c['config_id']} v{c['version']} 引用未知模块 {mid}")
        cert_id = c["electrical"]["certificate_id"]
        if cert_id not in book.certificates:
            errors.append(f"配置 {c['config_id']} v{c['version']} 引用未知证书 {cert_id}")

    for q in d["quotes"]:
        if q["inquiry_id"] not in book.inquiries:
            errors.append(f"报价 {q['id']} 引用未知询盘")
        for rev in q["revisions"]:
            for line in rev["lines"]:
                try:
                    book.config(line["config_id"], line["config_version"])
                except KeyError:
                    errors.append(
                        f"报价 {q['id']} r{rev['revision']} 引用未知配置 "
                        f"{line['config_id']} v{line['config_version']}")

    for o in d["orders"]:
        if o["inquiry_id"] not in book.inquiries:
            errors.append(f"订单 {o['id']} 引用未知询盘")
        if o["station_id"] not in book.stations:
            errors.append(f"订单 {o['id']} 引用未知服务站")
        try:
            book.quote_revision(o["signed_quote"]["quote_id"],
                                o["signed_quote"]["revision"])
        except KeyError as exc:
            errors.append(str(exc))
        for line in o["lines"]:
            if line["model_id"] not in book.products:
                errors.append(f"订单 {o['id']} 行 {line['line_id']} 引用未知户型")
            for key in ("signed_config", "current_config"):
                ref = line[key]
                try:
                    book.config(ref["config_id"], ref["version"])
                except KeyError:
                    errors.append(f"订单 {o['id']} {key} 指向未知配置 {ref}")

    for ecn in d["config_changes"]:
        ref = ecn["line_ref"]
        try:
            line = book.order_line(ref["order_id"], ref["line_id"])
        except KeyError as exc:
            errors.append(str(exc))
            continue
        cid = line["current_config"]["config_id"]
        for v in (ecn["from_version"], ecn["to_version"]):
            if (cid, v) not in book.configs:
                errors.append(f"变更 {ecn['id']} 引用 {cid} v{v} 不存在")

    for lk in d["capacity_locks"]:
        if lk["module_id"] not in book.modules:
            errors.append(f"锁 {lk['id']} 引用未知模块")
        ref = lk["line_ref"]
        try:
            book.order_line(ref["order_id"], ref["line_id"])
        except KeyError as exc:
            errors.append(str(exc))

    for p in d["production"]:
        if p["order_id"] not in book.orders:
            errors.append(f"序列号 {p['serial_no']} 引用未知订单")
        ref = p["frozen_config"]
        if (ref["config_id"], ref["version"]) not in book.configs:
            errors.append(f"序列号 {p['serial_no']} 冻结配置不存在")

    for ins in d["inspections"]:
        if ins["serial_no"] not in book.serials:
            errors.append(f"检验 {ins['id']} 引用未知序列号")
        if ins["ncr_id"] and ins["ncr_id"] not in book.ncrs:
            errors.append(f"检验 {ins['id']} 引用未知 NCR")

    for n in d["ncrs"]:
        if n["serial_no"] not in book.serials:
            errors.append(f"NCR {n['id']} 引用未知序列号")

    for c in d["containers"]:
        for s in c["serials"]:
            if s not in book.serials:
                errors.append(f"柜 {c['id']} 装入未知序列号 {s}")

    batch_ids = {s["batch_id"] for s in d["shipments"]}
    for c in d["containers"]:
        if c["batch_id"] not in batch_ids:
            errors.append(f"柜 {c['id']} 的批次 {c['batch_id']} 没有发运记录")
    for s in d["shipments"]:
        if s["order_id"] not in book.orders:
            errors.append(f"发运 {s['batch_id']} 引用未知订单")
        for cid in s["containers"]:
            if not any(c["id"] == cid for c in d["containers"]):
                errors.append(f"发运 {s['batch_id']} 引用未知柜 {cid}")

    for m in d["damage_reports"]:
        if m["serial_no"] not in book.serials:
            errors.append(f"损伤报告 {m['id']} 引用未知序列号")

    for acc in d["acceptance_checks"]:
        if acc["serial_no"] not in book.serials:
            errors.append(f"验收 {acc['id']} 引用未知序列号")
        ref = acc["against_config"]
        if (ref["config_id"], ref["version"]) not in book.configs:
            errors.append(f"验收 {acc['id']} 核对的配置不存在")

    for pch in d["punch_items"]:
        if pch["serial_no"] not in book.serials:
            errors.append(f"整改 {pch['id']} 引用未知序列号")

    for w in d["warranties"]:
        if w["serial_no"] not in book.serials:
            errors.append(f"保修引用未知序列号 {w['serial_no']}")

    for ev in d["events"]:
        for s in ev.get("serials", []):
            if s not in book.serials:
                errors.append(f"事件 {ev['event_id']} 引用未知序列号 {s}")

    return errors
