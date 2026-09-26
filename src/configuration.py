"""工程配置规则：变更影响核算、版本冻结与签署快照。"""

from datetime import date

from .records import Book, parse_day


def change_impact(book: Book, ecn: dict) -> dict:
    """依据两个配置版本复算部件、价格、重量与交期差异。

    部件差异取模块清单之差；价格、重量、交期取配置版本口径
    （单房价格与总重含基础房与制造成本，不等于模块简单加总；
    且跨制式模块可能以不同币种定价）。ECN 必须与该结果一致，
    防止差异被漏报或谎报。
    """
    line_ref = ecn["line_ref"]
    line = book.order_line(line_ref["order_id"], line_ref["line_id"])
    config_id = line["current_config"]["config_id"]
    before = book.config(config_id, ecn["from_version"])
    after = book.config(config_id, ecn["to_version"])

    before_mods = set(before["modules"])
    after_mods = set(after["modules"])

    price_before = before["unit_price"]
    price_after = after["unit_price"]
    if price_before["currency"] != price_after["currency"]:
        raise ValueError(f"变更 {ecn['id']} 配置版本跨币种，需先给汇率")

    return {
        "added_modules": sorted(after_mods - before_mods),
        "removed_modules": sorted(before_mods - after_mods),
        "price_delta": {
            "amount": price_after["amount"] - price_before["amount"],
            "currency": price_after["currency"],
        },
        "weight_delta_kg": after["weight_kg"] - before["weight_kg"],
        "lead_time_delta_days": after["lead_time_days"] - before["lead_time_days"],
    }


def impact_is_faithful(book: Book, ecn: dict) -> bool:
    """ECN 声明的影响必须与按模块目录复算的结果完全一致。"""
    actual = change_impact(book, ecn)
    declared = ecn["impact"]
    return (
        actual["added_modules"] == declared["added_modules"]
        and actual["removed_modules"] == declared["removed_modules"]
        and actual["price_delta"] == declared["price_delta"]
        and actual["weight_delta_kg"] == declared["weight_delta_kg"]
        and actual["lead_time_delta_days"] == declared["lead_time_delta_days"]
    )


def production_start(book: Book, serial_no: str) -> date | None:
    record = book.serials[serial_no]
    return parse_day(record.get("started_on"))


def loading_date(book: Book, serial_no: str) -> date | None:
    container = book.serial_container(serial_no)
    return parse_day(container["sealed_on"]) if container else None


def change_is_allowed(book: Book, ecn: dict, serials: list[str]) -> tuple[bool, str]:
    """投产/装柜后的数量不得静默切换版本。

    规则：
    - 任一相关序列号已投产，变更必须有客户书面批准，否则拒绝；
    - 任一相关序列号已装柜，任何换版一律拒绝；
    - 投产后获批的新版本只适用于未投产/未装柜的部分。
    """
    decided = parse_day(ecn["decided_on"])
    for serial_no in serials:
        loaded = loading_date(book, serial_no)
        if loaded is not None and loaded <= decided:
            return False, f"{serial_no} 已于 {loaded} 装柜，数量锁定，拒绝换版"
        started = production_start(book, serial_no)
        if started is not None and started <= decided:
            if not ecn.get("customer_approval_ref"):
                return False, f"{serial_no} 已于 {started} 投产，无客户书面批准，拒绝静默换版"
    return True, "允许在投产前变更"


def signed_snapshot(book: Book, serial_no: str) -> dict:
    """序列号出厂配置必须与制造时冻结版本一致，且不受后续报价修改影响。

    服务站扫码看到的是这份快照：电气制式、防风抗震、内装与智能模块。
    """
    record = book.serials[serial_no]
    cfg = book.config(record["frozen_config"]["config_id"],
                      record["frozen_config"]["version"])
    return {
        "serial_no": serial_no,
        "config_id": cfg["config_id"],
        "version": cfg["version"],
        "electrical": cfg["electrical"],
        "wind_rating": cfg["wind_rating"],
        "seismic_rating": cfg["seismic_rating"],
        "interior_package": cfg["interior_package"],
        "modules": list(cfg["modules"]),
        "smart_modules": list(cfg["smart_modules"]),
    }


def evaluate_changes(book: Book) -> list[dict]:
    """对每条 ECN 给出规则裁决，与样例中记录的 state 交叉验证。"""
    verdicts = []
    for ecn in book.changes:
        serials = [p["serial_no"] for p in
                   book.line_serials(ecn["line_ref"]["order_id"], ecn["line_ref"]["line_id"])]
        allowed, reason = change_is_allowed(book, ecn, serials)
        state = ecn["state"]
        consistent = (
            (allowed and state == "approved_before_production")
            or (not allowed and state.startswith("rejected"))
        )
        verdicts.append({
            "ecn_id": ecn["id"],
            "allowed": allowed,
            "reason": reason,
            "impact_faithful": impact_is_faithful(book, ecn),
            "state_consistent": consistent,
            "state": state,
        })
    return verdicts
