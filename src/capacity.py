"""产能锁定：多张订单争抢同一模块时，只能依据已确认产能锁定。"""

from .records import Book, parse_day


def _windows_overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start <= b_end and b_start <= a_end


def confirmed_capacity(book: Book, supplier_id: str, module_id: str,
                       start, end) -> int:
    """汇总供应商在给定时间窗内（窗口相交部分）已确认的产能数量。

    样例按整月窗口确认，相交即视为该数量可用于该窗；粒度更细时
    应按天比例拆分，这里保持与契约一致的整窗口径。
    """
    supplier = book.suppliers[supplier_id]
    total = 0
    for w in supplier["confirmed_capacity"]:
        if w["module_id"] != module_id:
            continue
        ws = parse_day(w["window_start"])
        we = parse_day(w["window_end"])
        if _windows_overlap(start, end, ws, we):
            total += w["qty"]
    return total


def held_quantity(book: Book, module_id: str, start, end,
                  exclude_lock: str | None = None) -> int:
    """同一模块、时间窗相交且处于 held 的锁定量。"""
    total = 0
    for lk in book.locks:
        if lk["module_id"] != module_id or lk["status"] != "held":
            continue
        if exclude_lock and lk["id"] == exclude_lock:
            continue
        if _windows_overlap(start, end, parse_day(lk["window_start"]),
                            parse_day(lk["window_end"])):
            total += lk["qty"]
    return total


def evaluate_lock(book: Book, lock: dict) -> tuple[bool, str]:
    """返回该锁在已确认产能约束下是否可以成立。"""
    if lock["basis"] != "confirmed":
        return False, f"锁 {lock['id']} 不是基于已确认产能"
    module = book.modules[lock["module_id"]]
    start = parse_day(lock["window_start"])
    end = parse_day(lock["window_end"])
    capacity = confirmed_capacity(book, module["supplier_id"],
                                  lock["module_id"], start, end)
    used = held_quantity(book, lock["module_id"], start, end,
                         exclude_lock=lock["id"])
    if used + lock["qty"] <= capacity:
        return True, f"窗口内已确认 {capacity}，已锁 {used}，本锁 {lock['qty']} 可成立"
    return False, (f"窗口内已确认 {capacity}，已锁 {used}，"
                   f"本锁 {lock['qty']} 将超产能")


def evaluate_all_locks(book: Book) -> list[dict]:
    """校验每条锁的记录状态与产能核算一致。"""
    results = []
    for lk in book.locks:
        feasible, reason = evaluate_lock(book, lk)
        if lk["status"] == "held":
            consistent = feasible
        elif lk["status"] == "rejected_over_capacity":
            consistent = not feasible
        else:
            consistent = False
        results.append({
            "lock_id": lk["id"],
            "module_id": lk["module_id"],
            "qty": lk["qty"],
            "status": lk["status"],
            "feasible": feasible,
            "status_consistent": consistent,
            "reason": reason,
        })
    return results
