"""折叠房跨境交付台账：结构校验、业务不变量与序列号追溯。

结构以 ``contracts/ledger.schema.json`` 为准（自包含的 JSON Schema 子集求值器，
不依赖第三方库）；业务不变量见 ``docs/domain-model.md`` 第 11 节。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "ledger.schema.json"

_SCHEMA_TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


class LedgerError(ValueError):
    """台账违反结构合约或业务不变量。"""


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return _SCHEMA_TYPES.get(type(value), "unknown")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _check_format(fmt: str, value: str) -> bool:
    try:
        if fmt == "date-time":
            _parse_dt(value)
            return True
        if fmt == "date":
            date.fromisoformat(value)
            return True
    except ValueError:
        return False
    return True


def _schema_errors(schema: dict, value: object, path: str, root: dict) -> list[str]:
    """求值本项目合约用到的 JSON Schema 子集。"""
    if "$ref" in schema:
        ref = schema["$ref"].lstrip("#/").split("/")
        target = root
        for part in ref:
            target = target[part]
        return _schema_errors(target, value, path, root)

    errors: list[str] = []
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if types and _json_type(value) not in types and not (
        _json_type(value) == "integer" and "number" in types
    ):
        errors.append(f"{path or '$'} 类型应为 {expected}，实际为 {_json_type(value)}")
        return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} 必须等于 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 的值 {value!r} 不在允许范围内")
    if "pattern" in schema and isinstance(value, str) and not re.fullmatch(schema["pattern"], value):
        errors.append(f"{path} 不匹配格式 {schema['pattern']}")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path} 长度不足 {schema['minLength']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            errors.append(f"{path} 小于最小值 {schema['minimum']}")
    if "minItems" in schema and isinstance(value, list) and len(value) < schema["minItems"]:
        errors.append(f"{path} 至少需要 {schema['minItems']} 项")
    if "format" in schema and isinstance(value, str) and not _check_format(schema["format"], value):
        errors.append(f"{path} 不是合法的 {schema['format']}：{value!r}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}" if path else f"$.{key} 为必填项")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                errors += _schema_errors(sub, value[key], f"{path}.{key}", root)
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                errors.append(f"{path} 出现未定义字段：{sorted(extra)}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += _schema_errors(schema["items"], item, f"{path}[{index}]", root)

    return errors


# ---------------------------------------------------------------- 业务不变量

def _option_index(ledger: dict) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for module in ledger["modules"]:
        for option in module["options"]:
            index[(module["module_id"], option["option_id"])] = option
    return index


def _events_by_id(order: dict) -> dict[str, dict]:
    return {event["event_id"]: event for event in order["events"]}


def _require_event(order: dict, event_id: str, *, stage: str | None = None,
                   at: str | None = None, label: str = "") -> None:
    events = _events_by_id(order)
    prefix = label or event_id
    if event_id not in events:
        raise LedgerError(f"{order['order_id']}：{prefix} 引用的事件 {event_id} 不存在")
    event = events[event_id]
    if stage and event["stage"] != stage:
        raise LedgerError(
            f"{order['order_id']}：{prefix} 事件 {event_id} 阶段应为 {stage}，实际为 {event['stage']}"
        )
    if at and event["at"] != at:
        raise LedgerError(
            f"{order['order_id']}：{prefix} 事件时间 {event['at']} 与记录时间 {at} 不一致"
        )
    for key in ("actor", "role", "decided_by"):
        if not event.get(key):
            raise LedgerError(f"{order['order_id']}：事件 {event_id} 缺少责任留痕字段 {key}")


def _validate_quotation(order: dict) -> None:
    quotation = order["quotation"]
    accepted = quotation["accepted"]
    line = next((line for line in quotation["lines"]
                 if line["currency"] == accepted["currency"]), None)
    if line is None:
        raise LedgerError(f"{order['order_id']}：接受币种 {accepted['currency']} 不在报价中")
    accepted_at = _parse_dt(accepted["accepted_at"])
    if accepted_at > _parse_dt(line["valid_until"]):
        raise LedgerError(
            f"{order['order_id']}：{accepted['currency']} 报价已于 {line['valid_until']} 到期，"
            f"不能在 {accepted['accepted_at']} 被接受"
        )


def _validate_revisions_and_changes(order: dict, options: dict[tuple[str, str], dict]) -> dict[int, dict]:
    revisions = order["configuration_revisions"]
    numbers = [revision["revision"] for revision in revisions]
    if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
        raise LedgerError(f"{order['order_id']}：配置版本号必须唯一且递增")
    signed = [revision for revision in revisions if revision["status"] == "signed"]
    if len(signed) != 1:
        raise LedgerError(f"{order['order_id']}：必须恰好有一个签署版本，实际有 {len(signed)} 个")
    by_number = {revision["revision"]: revision for revision in revisions}

    for revision in revisions:
        total_weight = 0.0
        option_prices: dict[str, float] = {}
        for chosen in revision["options"]:
            key = (chosen["module_id"], chosen["option_id"])
            if key not in options:
                raise LedgerError(
                    f"{order['order_id']}：版本{revision['revision']} 引用了不存在的模块选项 {key}"
                )
            option = options[key]
            if order["destination_country"] not in option["markets"]:
                raise LedgerError(
                    f"{order['order_id']}：选项 {chosen['option_id']} 不适用于目的国 "
                    f"{order['destination_country']}"
                )
            total_weight += option["weight_kg"] * chosen["quantity"]
            for price in option["unit_prices"]:
                option_prices[price["currency"]] = (
                    option_prices.get(price["currency"], 0.0) + price["amount"] * chosen["quantity"]
                )
        if abs(total_weight - revision["totals"]["weight_kg"]) > 1e-6:
            raise LedgerError(
                f"{order['order_id']}：版本{revision['revision']} 汇总重量 "
                f"{revision['totals']['weight_kg']} 与选项明细 {total_weight} 不一致"
            )
        for money in revision["totals"]["price"]:
            if abs(option_prices.get(money["currency"], 0.0) - money["amount"]) > 1e-6:
                raise LedgerError(
                    f"{order['order_id']}：版本{revision['revision']} 币种 {money['currency']} "
                    f"汇总价 {money['amount']} 与选项明细 {option_prices.get(money['currency'])} 不一致"
                )

    for change in order.get("change_orders", []):
        # 变更影响四要素：部件、价格、重量、交期（合约保证键存在，这里保证内容有效且数值吻合）
        if not change["affected_parts"]:
            raise LedgerError(f"{order['order_id']}：变更 {change['change_id']} 未列出受影响部件")
        if not change["price_delta"]:
            raise LedgerError(f"{order['order_id']}：变更 {change['change_id']} 未列出价格影响")
        old_rev, new_rev = by_number.get(change["from_revision"]), by_number.get(change["to_revision"])
        if old_rev is None or new_rev is None:
            raise LedgerError(f"{order['order_id']}：变更 {change['change_id']} 引用了不存在的版本")
        weight_delta = new_rev["totals"]["weight_kg"] - old_rev["totals"]["weight_kg"]
        if abs(weight_delta - change["weight_delta_kg"]) > 1e-6:
            raise LedgerError(
                f"{order['order_id']}：变更 {change['change_id']} 重量影响 "
                f"{change['weight_delta_kg']} 与版本差值 {weight_delta} 不一致"
            )
        old_prices = {m["currency"]: m["amount"] for m in old_rev["totals"]["price"]}
        new_prices = {m["currency"]: m["amount"] for m in new_rev["totals"]["price"]}
        for delta in change["price_delta"]:
            actual = new_prices.get(delta["currency"], 0.0) - old_prices.get(delta["currency"], 0.0)
            if abs(actual - delta["amount"]) > 1e-6:
                raise LedgerError(
                    f"{order['order_id']}：变更 {change['change_id']} {delta['currency']} 价格影响 "
                    f"{delta['amount']} 与版本差值 {actual} 不一致"
                )
        delivery_delta = (
            date.fromisoformat(new_rev["totals"]["earliest_delivery"])
            - date.fromisoformat(old_rev["totals"]["earliest_delivery"])
        ).days
        if delivery_delta != change["delivery_delta_days"]:
            raise LedgerError(
                f"{order['order_id']}：变更 {change['change_id']} 交期影响 "
                f"{change['delivery_delta_days']}天 与版本差值 {delivery_delta}天 不一致"
            )
        for approver_key in ("approved_by_customer", "approved_by_engineering"):
            if not change.get(approver_key):
                raise LedgerError(f"{order['order_id']}：变更 {change['change_id']} 缺少 {approver_key}")

    return by_number


def _validate_certificates(order: dict, first_production_at: datetime) -> None:
    if not order["certificates"]:
        raise LedgerError(f"{order['order_id']}：缺少目的国证书记录")
    latest_review = None
    for certificate in order["certificates"]:
        if certificate["country"] != order["destination_country"]:
            raise LedgerError(
                f"{order['order_id']}：证书 {certificate['certificate_id']} 适用国家与目的国不符"
            )
        if certificate["status"] != "approved":
            raise LedgerError(
                f"{order['order_id']}：证书 {certificate['certificate_id']} 状态为 "
                f"{certificate['status']}，未通过评审不得投产"
            )
        if not certificate["access_grants"]:
            raise LedgerError(
                f"{order['order_id']}：证书 {certificate['certificate_id']} 缺少访问授权留痕"
            )
        reviewed_at = _parse_dt(certificate["reviewed_at"])
        latest_review = reviewed_at if latest_review is None else max(latest_review, reviewed_at)
    if latest_review and first_production_at < latest_review:
        raise LedgerError(
            f"{order['order_id']}：投产时间 {first_production_at} 早于证书评审完成时间 {latest_review}"
        )


def _validate_order(ledger: dict, order: dict, options: dict[tuple[str, str], dict],
                    slots: dict[str, dict]) -> dict[str, dict]:
    oid = order["order_id"]
    if order["customer_id"] not in {c["customer_id"] for c in ledger["customers"]}:
        raise LedgerError(f"{oid}：引用了不存在的客户")

    _validate_quotation(order)
    revisions = _validate_revisions_and_changes(order, options)

    # 事件链：唯一、按时间排序、引用完整
    event_ids = [event["event_id"] for event in order["events"]]
    if len(event_ids) != len(set(event_ids)):
        raise LedgerError(f"{oid}：事件 ID 重复")
    timestamps = [_parse_dt(event["at"]) for event in order["events"]]
    if timestamps != sorted(timestamps):
        raise LedgerError(f"{oid}：事件链未按时间排序")
    for event in order["events"]:
        for key in ("actor", "role", "decided_by", "summary"):
            if not event.get(key):
                raise LedgerError(f"{oid}：事件 {event['event_id']} 缺少 {key}")

    # 产能锁：槽位存在、模块选项一致、状态合法
    for lock in order["capacity_locks"]:
        slot = slots.get(lock["slot_id"])
        if slot is None:
            raise LedgerError(f"{oid}：产能锁 {lock['lock_id']} 引用不存在的槽位 {lock['slot_id']}")
        if (slot["module_id"], slot["option_id"]) != (lock["module_id"], lock["option_id"]):
            raise LedgerError(f"{oid}：产能锁 {lock['lock_id']} 与槽位模块选项不一致")

    # 批次、序列号与投产冻结
    produced: dict[str, dict] = {}
    first_production_at: datetime | None = None
    for batch in order["batches"]:
        if batch["quantity"] != len(batch["units"]):
            raise LedgerError(
                f"{oid}：批次 {batch['batch_id']} 投产数量 {batch['quantity']} "
                f"与实际序列号数 {len(batch['units'])} 不符（投产数量不得静默变化）"
            )
        revision = revisions.get(batch["revision"])
        # 批次必须依据"已签署"的版本（显式换版后，历史批次保留其当时签署的旧版本 as-built）；
        # 但如果该版本在投产之前就已被变更单取代，则属于静默按旧版投产，必须拒绝。
        if revision is None or not revision.get("signed_by") or not revision.get("signed_at"):
            raise LedgerError(
                f"{oid}：批次 {batch['batch_id']} 必须依据经双方签署的配置版本生产，"
                f"实际指向版本 {batch['revision']}（草稿不得投产）"
            )
        released_at = _parse_dt(batch["production_released_at"])
        if revision["status"] == "superseded":
            supersede_times = [
                _parse_dt(change["approved_at"])
                for change in order.get("change_orders", [])
                if change["from_revision"] == batch["revision"]
            ]
            if not supersede_times or min(supersede_times) <= released_at:
                raise LedgerError(
                    f"{oid}：批次 {batch['batch_id']} 所依据的版本 {batch['revision']} "
                    f"在投产前已被新版本取代（禁止静默切换版本；换版须走显式变更并重签）"
                )
        first_production_at = released_at if first_production_at is None else min(
            first_production_at, released_at
        )
        _require_event(order, batch["release_event_id"], stage="production",
                       at=batch["production_released_at"], label=f"批次 {batch['batch_id']} 投产")
        for unit in batch["units"]:
            serial = unit["serial_number"]
            if serial in produced:
                raise LedgerError(f"{oid}：序列号 {serial} 在同一订单内重复")
            produced[serial] = {"batch": batch, "unit": unit}
            _require_event(order, unit["minted_event_id"], stage="qc",
                           label=f"序列号 {serial} 打刻")
            if _parse_dt(_events_by_id(order)[unit["minted_event_id"]]["at"]) < released_at:
                raise LedgerError(f"{oid}：序列号 {serial} 打刻早于所在批次投产")
    if sum(b["quantity"] for b in order["batches"]) < order["quantity"]:
        raise LedgerError(f"{oid}：投产总数少于订单数量")

    _validate_certificates(order, first_production_at or _parse_dt("9999-01-01T00:00:00Z"))

    # 质检：未闭环不合格项不得装柜
    open_qc = {
        serial
        for serial, info in produced.items()
        for item in info["unit"]["qc_items"]
        if item["result"] in ("concession", "reject") and not item["closed"]
    }

    # 装柜冻结：箱内序列号必须存在，且版本与其批次一致
    container_serials: set[str] = set()
    for container in order["containers"]:
        _require_event(order, container["loading_event_id"], stage="loading",
                       at=container["sealed_at"], label=f"集装箱 {container['container_no']} 装柜")
        if not container["serials"]:
            raise LedgerError(f"{oid}：集装箱 {container['container_no']} 内容为空")
        for serial in container["serials"]:
            if serial not in produced:
                raise LedgerError(f"{oid}：集装箱装入了不存在的序列号 {serial}")
            if produced[serial]["batch"]["revision"] != container["revision"]:
                raise LedgerError(
                    f"{oid}：装柜后序列号 {serial} 的版本与箱封版本不一致（禁止静默调换）"
                )
            if serial in open_qc:
                raise LedgerError(f"{oid}：序列号 {serial} 存在未闭环质检不合格项，不得装柜")
        container_serials.update(container["serials"])
    if set(produced) != container_serials:
        raise LedgerError(
            f"{oid}：已生产序列号与已装柜序列号不一致 "
            f"(缺装 {sorted(set(produced) - container_serials)}, "
            f"多装 {sorted(container_serials - set(produced))})"
        )

    # 发运：分批完整覆盖装柜集合，事件时间合法
    shipped_serials: set[str] = set()
    container_nos = {c["container_no"] for c in order["containers"]}
    previous_shipped = None
    for shipment in sorted(order["shipments"], key=lambda s: s["seq"]):
        for number in shipment["container_nos"]:
            if number not in container_nos:
                raise LedgerError(f"{oid}：发运 {shipment['shipment_id']} 引用不存在的集装箱 {number}")
        for serial in shipment["serials"]:
            if serial not in container_serials:
                raise LedgerError(f"{oid}：发运包含未装柜序列号 {serial}")
            if serial in shipped_serials:
                raise LedgerError(f"{oid}：序列号 {serial} 被重复发运")
        shipped_serials.update(shipment["serials"])
        _require_event(order, shipment["shipped_event_id"], stage="shipping",
                       at=shipment["shipped_at"], label=f"发运 {shipment['shipment_id']} 离港")
        _require_event(order, shipment["arrived_event_id"], stage="arrival",
                       at=shipment["arrived_at"], label=f"发运 {shipment['shipment_id']} 到港")
        shipped_at = _parse_dt(shipment["shipped_at"])
        if shipped_at > _parse_dt(shipment["arrived_at"]):
            raise LedgerError(f"{oid}：发运 {shipment['shipment_id']} 到港早于离港")
        if previous_shipped and shipped_at < previous_shipped:
            raise LedgerError(f"{oid}：发运批次顺序号与离港时间不一致")
        previous_shipped = shipped_at
    if shipped_serials != container_serials:
        raise LedgerError(f"{oid}：发运序列号集合与装柜集合不一致")

    # 异常单：序列号存在，闭环信息自洽
    for exception in order.get("exceptions", []):
        if exception["serial_number"] not in produced:
            raise LedgerError(f"{oid}：异常单 {exception['exception_id']} 引用不存在的序列号")
        _require_event(order, exception["opened_event_id"], label=f"异常 {exception['exception_id']} 开立")
        if exception["closed"]:
            if not exception.get("closed_event_id") or not exception.get("closed_at"):
                raise LedgerError(f"{oid}：异常单 {exception['exception_id']} 标记闭环但缺少闭环记录")
            _require_event(order, exception["closed_event_id"],
                           label=f"异常 {exception['exception_id']} 闭环")

    # 断网安装：验收完成前必须回传并勾对
    acceptance = order["acceptance"]
    completed_at = _parse_dt(acceptance["completed_at"])
    for offline in order.get("offline_installs", []):
        if offline["shipment_id"] not in {s["shipment_id"] for s in order["shipments"]}:
            raise LedgerError(f"{oid}：离线安装记录 {offline['offline_id']} 引用不存在的发运批次")
        for serial in offline["serials"]:
            if serial not in produced:
                raise LedgerError(f"{oid}：离线安装记录引用不存在的序列号 {serial}")
        captured_at = _parse_dt(offline["captured_at"])
        if offline["synced_at"] is None or offline["reconciled_at"] is None:
            raise LedgerError(
                f"{oid}：离线安装记录 {offline['offline_id']} 尚未回传勾对，验收不得完成"
            )
        reconciled_at = _parse_dt(offline["reconciled_at"])
        if reconciled_at < captured_at or reconciled_at > completed_at:
            raise LedgerError(
                f"{oid}：离线记录 {offline['offline_id']} 勾对时间必须介于采集与验收完成之间"
            )
        _require_event(order, offline["sync_event_id"], stage="installation",
                       at=offline["synced_at"], label=f"离线记录 {offline['offline_id']} 回传")

    # 验收：逐项核对实物与签署版本
    _require_event(order, acceptance["acceptance_event_id"], stage="acceptance",
                   at=acceptance["completed_at"], label="验收完成")
    accepted_serials = {item["serial_number"] for item in acceptance["items"]}
    if accepted_serials != set(produced):
        raise LedgerError(f"{oid}：验收序列号集合与已生产序列号集合不一致")
    signed_at = _parse_dt(acceptance["signed_at"])
    if signed_at > completed_at:
        raise LedgerError(f"{oid}：客户签署晚于验收完成")
    last_arrival = max(_parse_dt(s["arrived_at"]) for s in order["shipments"])
    if signed_at < last_arrival:
        raise LedgerError(f"{oid}：客户签署早于末批到港时间")
    for item in acceptance["items"]:
        serial = item["serial_number"]
        if item["revision"] != produced[serial]["batch"]["revision"]:
            raise LedgerError(
                f"{oid}：验收时序列号 {serial} 标注版本与实物批次版本不符"
            )
        # 逐项核对以该座房屋 as-built 的签署版本为准（显式换版后各批版本可以不同）
        as_built_options = {
            (chosen["module_id"], chosen["option_id"])
            for chosen in revisions[item["revision"]]["options"]
        }
        checked = {(check["module_id"], check["option_id"]) for check in item["checks"]}
        if checked != as_built_options:
            raise LedgerError(
                f"{oid}：序列号 {serial} 验收项与其签署版本选项不一致 "
                f"(缺 {sorted(as_built_options - checked)}, 多 {sorted(checked - as_built_options)})"
            )
        for check in item["checks"]:
            if check["result"] != "matched":
                raise LedgerError(f"{oid}：序列号 {serial} 验收项 {check['module_id']} 未通过")

    # 保修：序列号存在，开立事件留痕
    for claim in order.get("warranty_claims", []):
        if claim["serial_number"] not in produced:
            raise LedgerError(f"{oid}：保修单 {claim['claim_id']} 引用不存在的序列号")
        _require_event(order, claim["opened_event_id"], stage="warranty",
                       at=claim["opened_at"], label=f"保修单 {claim['claim_id']} 开立")

    return produced


def _validate_capacity(ledger: dict) -> None:
    slots = {slot["slot_id"]: slot for slot in ledger["capacity_slots"]}
    consumed: dict[str, int] = {slot_id: 0 for slot_id in slots}
    for order in ledger["orders"]:
        for lock in order["capacity_locks"]:
            if lock["status"] == "active":
                consumed[lock["slot_id"]] = consumed.get(lock["slot_id"], 0) + lock["qty"]
    for slot_id, qty in consumed.items():
        if qty > slots[slot_id]["confirmed_qty"]:
            raise LedgerError(
                f"槽位 {slot_id} 已确认产能 {slots[slot_id]['confirmed_qty']}，"
                f"被有效锁定 {qty}（产能超卖；多单争抢须按已确认产能锁定）"
            )


def validate_ledger(ledger: dict) -> dict:
    """校验结构合约与全部业务不变量，通过时返回台账本身。"""
    schema = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    structural = _schema_errors(schema, ledger, "$", schema)
    if structural:
        raise LedgerError("台账结构不符合合约：\n  - " + "\n  - ".join(structural))

    options = _option_index(ledger)
    slots = {slot["slot_id"]: slot for slot in ledger["capacity_slots"]}

    all_serials: dict[str, str] = {}
    for order in ledger["orders"]:
        produced = _validate_order(ledger, order, options, slots)
        for serial in produced:
            if serial in all_serials:
                raise LedgerError(
                    f"序列号 {serial} 被两座房屋重复使用（{all_serials[serial]} 与 {order['order_id']}）"
                )
            all_serials[serial] = order["order_id"]

    _validate_capacity(ledger)
    return ledger


def load_ledger(path: str | Path) -> dict:
    """读取、解析并校验台账文件。"""
    ledger = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_ledger(ledger)


# ---------------------------------------------------------------- 序列号追溯

def serial_trace(ledger: dict, serial_number: str) -> list[dict]:
    """按时间返回某座房屋从打刻到保修的完整决定链，供海外服务站扫码重现。"""
    owning_order = None
    for candidate in ledger["orders"]:
        if any(
            unit["serial_number"] == serial_number
            for batch in candidate["batches"]
            for unit in batch["units"]
        ):
            owning_order = candidate
            break
    if owning_order is None:
        raise LedgerError(f"序列号 {serial_number} 不存在")

    serial_events = [
        {
            "at": event["at"],
            "stage": event["stage"],
            "type": event["type"],
            "actor": event["actor"],
            "role": event["role"],
            "decided_by": event["decided_by"],
            "summary": event["summary"],
        }
        for event in owning_order["events"]
        if event.get("serial_number") == serial_number
    ]

    # 容器/发运/验收等事件不逐座携带序列号，按箱内清单与时间窗补入
    batches = {
        unit["serial_number"]: batch
        for batch in owning_order["batches"]
        for unit in batch["units"]
    }
    batch = batches[serial_number]
    for container in owning_order["containers"]:
        if serial_number in container["serials"]:
            event = _events_by_id(owning_order)[container["loading_event_id"]]
            serial_events.append(_trace_entry(event))
            for shipment in owning_order["shipments"]:
                if container["container_no"] in shipment["container_nos"]:
                    events = _events_by_id(owning_order)
                    serial_events.append(_trace_entry(events[shipment["shipped_event_id"]]))
                    serial_events.append(_trace_entry(events[shipment["arrived_event_id"]]))
    for offline in owning_order.get("offline_installs", []):
        if serial_number in offline["serials"] and offline["sync_event_id"]:
            serial_events.append(_trace_entry(_events_by_id(owning_order)[offline["sync_event_id"]]))
    serial_events.append(_trace_entry(_events_by_id(owning_order)[
        owning_order["acceptance"]["acceptance_event_id"]
    ]))
    for claim in owning_order.get("warranty_claims", []):
        if claim["serial_number"] == serial_number:
            serial_events.append(_trace_entry(_events_by_id(owning_order)[claim["opened_event_id"]]))

    serial_events.sort(key=lambda event: event["at"])
    # 同一事件可能既逐座携带序列号、又经保修循环补入，按内容去重
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for entry in serial_events:
        key = (entry["at"], entry["stage"], entry["type"], entry["summary"])
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    serial_events = deduped

    # 附上扫码即见的配置四要素
    revision = next(
        r for r in owning_order["configuration_revisions"] if r["revision"] == batch["revision"]
    )
    serial_events.insert(0, {
        "at": "(configuration snapshot)",
        "stage": "configuration",
        "type": "as_built_snapshot",
        "actor": "",
        "role": "engineering",
        "decided_by": revision.get("signed_by", ""),
        "summary": (
            f"版本{revision['revision']}｜电气 {revision['electrical']['voltage']}/"
            f"{revision['electrical']['frequency_hz']}Hz {revision['electrical']['standard']}｜"
            f"防风抗震 {revision['wind_seismic_rating']['wind_kph']}kph/"
            f"{revision['wind_seismic_rating']['seismic']}｜内装：{revision['interior']}｜"
            f"选项：{[o['option_id'] for o in revision['options']]}"
        ),
    })
    return serial_events


def order_serials(ledger: dict) -> dict[str, list[str]]:
    return {
        order["order_id"]: [
            unit["serial_number"]
            for batch in order["batches"]
            for unit in batch["units"]
        ]
        for order in ledger["orders"]
    }


def _trace_entry(event: dict) -> dict:
    return {
        "at": event["at"],
        "stage": event["stage"],
        "type": event["type"],
        "actor": event["actor"],
        "role": event["role"],
        "decided_by": event["decided_by"],
        "summary": event["summary"],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="折叠房跨境交付台账校验与序列号追溯")
    parser.add_argument("path", type=Path, help="台账 JSON 文件")
    parser.add_argument("--trace", metavar="SERIAL", help="输出指定序列号的完整决定链")
    args = parser.parse_args(argv)

    ledger = load_ledger(args.path)
    if args.trace:
        for entry in serial_trace(ledger, args.trace):
            print(f"{entry['at']:>24}  {entry['stage']:<13} {entry['type']:<28} "
                  f"{entry['decided_by']:<24} {entry['summary']}")
        return 0

    order_count = len(ledger["orders"])
    serial_count = sum(len(serials) for serials in order_serials(ledger).values())
    print(f"校验通过：{order_count} 张订单，{serial_count} 座房屋，"
          f"产能槽 {len(ledger['capacity_slots'])} 个，事件链完整。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
