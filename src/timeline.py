"""序列号档案：从序列号重现制造到运维的每次决定与责任归属。"""

from .records import Book, parse_day
from .configuration import signed_snapshot


def expected_frozen_config(book: Book, order: dict, line: dict) -> dict:
    """投产冻结版本 = 签署版本，沿投产前已批准的 ECN 链推进后的版本。"""
    cid = line["signed_config"]["config_id"]
    version = line["signed_config"]["version"]
    ecns = sorted(
        (e for e in book.changes
         if e["line_ref"]["order_id"] == order["id"]
         and e["line_ref"]["line_id"] == line["line_id"]
         and e["state"] == "approved_before_production"),
        key=lambda e: parse_day(e["decided_on"]),
    )
    advanced = True
    while advanced:
        advanced = False
        for ecn in ecns:
            if ecn["from_version"] == version:
                version = ecn["to_version"]
                advanced = True
    return {"config_id": cid, "version": version}


def serial_dossier(book: Book, serial_no: str) -> dict:
    """聚合一台房从订单、配置冻结、质检、运输、验收至保修的完整轨迹。

    海外服务站扫码即得：
    - 出厂快照（电气制式、防风抗震、内装、智能模块）；
    - 逐事件决定链（时间、阶段、责任方）；
    - 异常与处理（NCR、运输损伤、整改）。
    """
    if serial_no not in book.serials:
        raise KeyError(f"未知序列号 {serial_no}")
    prod = book.serials[serial_no]
    order = book.orders[prod["order_id"]]
    line = next(l for l in order["lines"] if l["line_id"] == prod["line_id"])
    expected = expected_frozen_config(book, order, line)

    events = sorted(
        (e for e in book.data["events"] if serial_no in e.get("serials", [])),
        key=lambda e: e["ts"],
    )

    inspections = [i for i in book.data["inspections"] if i["serial_no"] == serial_no]
    ncrs = [n for n in book.data["ncrs"] if n["serial_no"] == serial_no]
    damages = [d for d in book.data["damage_reports"] if d["serial_no"] == serial_no]
    punches = [p for p in book.data["punch_items"] if p["serial_no"] == serial_no]
    acceptance = [a for a in book.data["acceptance_checks"] if a["serial_no"] == serial_no]
    warranty = next((w for w in book.data["warranties"] if w["serial_no"] == serial_no), None)

    shipment = book.serial_shipment(serial_no)
    station = book.stations[order["station_id"]]
    cfg = book.config(prod["frozen_config"]["config_id"], prod["frozen_config"]["version"])
    cert = book.certificates[cfg["electrical"]["certificate_id"]]

    return {
        "serial_no": serial_no,
        "product": book.products[line["model_id"]]["name"],
        "destination_country": order["destination_country"],
        "service_station": station["name"],
        "as_built": signed_snapshot(book, serial_no),
        "certificate": {
            "id": cert["id"], "title": cert["title"],
            "valid_until": cert["valid_until"],
            "accessible_at_this_station": cert["id"] in station["cert_access"],
            "doc_uri": cert["doc_uri"],
        },
        "order": {
            "id": order["id"],
            "signed_quote": order["signed_quote"],
            "signed_config": line["signed_config"],
            "expected_frozen": expected,
            "frozen_at_production": prod["frozen_config"],
            "frozen_chain_valid": prod["frozen_config"] == expected,
        },
        "production": {"started_on": prod["started_on"], "completed_on": prod["completed_on"]},
        "inspections": [
            {"id": i["id"], "stage": i["stage"], "on": i["on"],
             "result": i["result"], "ncr_id": i.get("ncr_id")}
            for i in sorted(inspections, key=lambda i: parse_day(i["on"]))
        ],
        "ncrs": ncrs,
        "shipment": shipment,
        "damages": damages,
        "punches": punches,
        "acceptance": acceptance,
        "warranty": warranty,
        "decisions": [
            {"ts": e["ts"], "stage": e["stage"], "type": e["type"],
             "actor": e["actor"], "responsibility": e["responsibility"],
             "summary": e["summary"], "refs": e.get("refs", {})}
            for e in events
        ],
    }


def production_config_chain_valid(book: Book) -> list[dict]:
    """每台序列号的冻结配置必须等于签署版本，或经投产前批准的 ECN 衔接。"""
    results = []
    for prod in book.data["production"]:
        line = book.order_line(prod["order_id"], prod["line_id"])
        order = book.orders[prod["order_id"]]
        expected = expected_frozen_config(book, order, line)
        results.append({
            "serial_no": prod["serial_no"],
            "signed": line["signed_config"],
            "expected_frozen": expected,
            "frozen": prod["frozen_config"],
            "valid": prod["frozen_config"] == expected,
        })
    return results
