"""交付与售后规则：报价期限、证书访问、质检放行、分批、损伤、
许可、断网回传同步与逐项验收。"""

from collections import Counter
from datetime import date

from .records import Book, parse_day


# -- 报价 ----------------------------------------------------------------

def quote_is_valid(book: Book, quote_id: str, revision: int, on) -> tuple[bool, str]:
    """多币种报价必须在其版本声明的有效期内被接受，币种以该版本为准。"""
    rev = book.quote_revision(quote_id, revision)
    day = parse_day(on)
    start = parse_day(rev["valid_from"])
    end = parse_day(rev["valid_until"])
    currencies = {line["unit_price"]["currency"] for line in rev["lines"]}
    if rev["currency"] not in currencies:
        return False, f"报价 {quote_id} r{revision} 币种与行价格不一致"
    if not (start <= day <= end):
        return False, f"报价 {quote_id} r{revision} 于 {on} 已超出有效期 {start}~{end}"
    return True, f"{rev['currency']} 报价在有效期内"


def signed_price_matches_quote(book: Book, order: dict) -> bool:
    """订单行的签署配置必须引用报价当时版本中的同一配置版本与单价。"""
    rev = book.quote_revision(order["signed_quote"]["quote_id"],
                              order["signed_quote"]["revision"])
    quote_lines = {line["line_id"]: line for line in rev["lines"]}
    for line in order["lines"]:
        ql = quote_lines.get(line["line_id"])
        if not ql:
            return False
        sc = line["signed_config"]
        if (ql["config_id"] != sc["config_id"]
                or ql["config_version"] != sc["version"]):
            return False
        cfg = book.config(sc["config_id"], sc["version"])
        if ql["unit_price"] != cfg["unit_price"]:
            return False
    return True


def order_signed_within_validity(book: Book, order: dict) -> bool:
    ok, _ = quote_is_valid(book, order["signed_quote"]["quote_id"],
                           order["signed_quote"]["revision"], order["signed_on"])
    return ok


# -- 合规证书 -------------------------------------------------------------

def certificate_covers(book: Book, certificate_id: str, country: str, on) -> bool:
    cert = book.certificates[certificate_id]
    day = parse_day(on)
    return (country in cert["countries"]
            and parse_day(cert["issued_on"]) <= day <= parse_day(cert["valid_until"]))


def station_can_access_cert(book: Book, station_id: str, certificate_id: str) -> bool:
    """目的国证书只向授权服务站开放，扫码即取。"""
    station = book.stations[station_id]
    return certificate_id in station["cert_access"]


def order_compliance(book: Book, order: dict) -> list[dict]:
    results = []
    for line in order["lines"]:
        sc = line["signed_config"]
        cfg = book.config(sc["config_id"], sc["version"])
        cert_id = cfg["electrical"]["certificate_id"]
        results.append({
            "line_id": line["line_id"],
            "certificate_id": cert_id,
            "covers_country": certificate_covers(book, cert_id,
                                                 order["destination_country"],
                                                 order["signed_on"]),
            "station_access": station_can_access_cert(book, order["station_id"], cert_id),
        })
    return results


# -- 质检放行 -------------------------------------------------------------

def latest_inspection(book: Book, serial_no: str, stage: str | None = None) -> dict:
    records = [i for i in book.data["inspections"] if i["serial_no"] == serial_no]
    if stage:
        records = [i for i in records if i["stage"] == stage]
    return max(records, key=lambda i: (parse_day(i["on"]), i["id"]))


def released_for_loading(book: Book, serial_no: str) -> tuple[bool, str]:
    """检验不合格（NCR 未关闭或最近一次出厂检验不合格）禁止装柜。

    出厂放行只看封条之前的终检/复验；站端到货、运输损伤修复等
    装柜之后的检验不属于此闸门。
    """
    gate_stages = {"final", "re-inspection"}
    records = [i for i in book.data["inspections"]
               if i["serial_no"] == serial_no and i["stage"] in gate_stages]
    container = book.serial_container(serial_no)
    if container:
        seal_day = parse_day(container["sealed_on"])
        records = [i for i in records if parse_day(i["on"]) <= seal_day]
    if not records:
        return False, f"{serial_no} 缺少出厂终检记录"
    last = max(records, key=lambda i: (parse_day(i["on"]), i["id"]))
    if last["result"] != "pass":
        return False, f"{serial_no} 最近出厂检验 {last['id']} 为 {last['result']}"
    if last.get("ncr_id"):
        ncr = book.ncrs[last["ncr_id"]]
        closed = parse_day(ncr.get("closed_on"))
        if closed is None or (container and closed > parse_day(container["sealed_on"])):
            return False, f"{serial_no} 的 {ncr['id']} 在装柜前未关闭"
    if container and not container.get("fold_verified"):
        return False, f"{serial_no} 装柜前未完成折叠核验"
    return True, f"{serial_no} 持合格终检放行"


def all_loaded_serials_released(book: Book) -> list[dict]:
    results = []
    for container in book.data["containers"]:
        for serial_no in container["serials"]:
            ok, reason = released_for_loading(book, serial_no)
            results.append({"serial_no": serial_no, "container": container["id"],
                            "released": ok, "reason": reason})
    return results


# -- 分批发运与数量 --------------------------------------------------------

def order_shipment_plan(book: Book, order_id: str) -> dict:
    """订单每一行的数量必须与实际装柜数量一致，且只能按批次分批，不得串单。"""
    order = book.orders[order_id]
    expected = {l["line_id"]: l["qty"] for l in order["lines"]}
    batches = order["batch_plan"]
    actual = Counter()
    seen_serials = []
    for batch in batches:
        containers = [c for c in book.data["containers"] if c["batch_id"] == batch]
        for c in containers:
            for serial_no in c["serials"]:
                prod = book.serials[serial_no]
                if prod["order_id"] != order_id:
                    actual[f"__foreign__:{serial_no}"] += 1
                else:
                    actual[prod["line_id"]] += 1
                    seen_serials.append(serial_no)
    matched = all(actual[line_id] == qty for line_id, qty in expected.items())
    return {
        "order_id": order_id,
        "batches": batches,
        "loaded_by_line": {k: v for k, v in actual.items() if not k.startswith("__foreign__")},
        "foreign_serials": [k.split(":", 1)[1] for k in actual if k.startswith("__foreign__")],
        "quantities_match": matched,
    }


# -- 运输损伤 -------------------------------------------------------------

def damage_resolution(book: Book, damage_id: str) -> dict:
    dmg = book.damages[damage_id]
    repair_inspections = [
        i for i in book.data["inspections"]
        if i["serial_no"] == dmg["serial_no"]
        and i["stage"] == "station-repair-verification"
        and parse_day(i["on"]) >= parse_day(dmg["found_on"])
    ]
    verified = bool(repair_inspections) and all(
        i["result"] == "pass" for i in repair_inspections)
    return {
        "damage_id": damage_id,
        "serial_no": dmg["serial_no"],
        "seals_intact_on_arrival": dmg["container_seals_intact"],
        "joint_survey_held": bool(dmg.get("joint_survey_on")),
        "responsibility": dmg["determined_responsibility"],
        "claim_opened": bool(dmg.get("claim_no")),
        "repair_verified": verified,
        "closed_loop": all([
            dmg["container_seals_intact"], dmg.get("joint_survey_on"),
            dmg.get("claim_no"), dmg.get("resolved_on"), verified,
        ]),
    }


# -- 许可与断网安装 --------------------------------------------------------

def permits_granted(book: Book, site_id: str, on) -> tuple[bool, list[str]]:
    pending = []
    for p in book.data["permits"]:
        if p["site_id"] != site_id:
            continue
        granted = parse_day(p.get("granted_on"))
        if granted is None or granted > parse_day(on):
            pending.append(p["type"])
    return not pending, pending


def reconcile_offline_records(book: Book) -> dict:
    """现场断网记录先存本地、恢复后回传。

    - 同一 client_event_id + 相同 payload_hash：去重，只生效一次；
    - 同一 client_event_id 但内容不同：冲突，隔离待人工裁决；
    - 送电类动作在许可未批时必须被阻断。
    """
    groups: dict[str, list[dict]] = {}
    for rec in book.data["offline_install_records"]:
        groups.setdefault(rec["client_event_id"], []).append(rec)

    unique, duplicates, conflicts = [], [], []
    for cid, records in groups.items():
        hashes = {r["payload_hash"] for r in records}
        if len(records) == 1:
            unique.append(records[0])
        elif len(hashes) == 1:
            kept = min(records, key=lambda r: r["synced_at"])
            unique.append(kept)
            duplicates.append({"client_event_id": cid,
                               "dropped_syncs": len(records) - 1})
        else:
            conflicts.append({"client_event_id": cid,
                              "payload_hashes": sorted(hashes)})

    blocked = []
    for rec in unique:
        if rec["type"] == "energization-blocked":
            granted, pending = permits_granted(book, rec["site_id"],
                                               parse_day(rec["recorded_at"]))
            if granted:
                blocked.append({"client_event_id": rec["client_event_id"],
                                "problem": "记录称被阻断但许可已齐备"})
        if rec["type"] == "energized":
            granted, pending = permits_granted(book, rec["site_id"],
                                               parse_day(rec["recorded_at"]))
            if not granted:
                blocked.append({"client_event_id": rec["client_event_id"],
                                "problem": f"无许可送电，缺 {pending}"})

    # 实际送电时间不得早于任一许可批准时间
    energization_ok = True
    for inst in book.data["installations"]:
        granted, pending = permits_granted(book, inst["site_id"],
                                           parse_day(inst["energized_on"]))
        if not granted:
            energization_ok = False

    return {
        "unique_records": len(unique),
        "duplicates_collapsed": duplicates,
        "conflicts_quarantined": conflicts,
        "permit_violations": blocked,
        "energization_after_permits": energization_ok,
    }


# -- 逐项验收 -------------------------------------------------------------

def evaluate_acceptance(book: Book, acceptance_id: str) -> dict:
    """客户按签署（冻结）版本逐项核对实物；有未关闭不符项不得签收。"""
    acc = next(a for a in book.data["acceptance_checks"] if a["id"] == acceptance_id)
    mismatches = [it for it in acc["items"] if it["result"] != "match"]
    open_punches = []
    for it in mismatches:
        if it.get("punch_id"):
            pch = book.punches[it["punch_id"]]
            if not pch.get("closed_on") or pch["id"] not in acc.get("resolved_punch", []):
                open_punches.append(it["punch_id"])
    may_sign = not mismatches or (not open_punches and acc.get("resolved_punch"))
    signed_valid = acc["signed_by_customer"] == may_sign and (
        not acc["signed_by_customer"] or bool(acc.get("signed_on")))
    return {
        "acceptance_id": acceptance_id,
        "serial_no": acc["serial_no"],
        "against": acc["against_config"],
        "mismatches": mismatches,
        "open_punches": open_punches,
        "may_sign": may_sign,
        "signed_valid": signed_valid,
    }
