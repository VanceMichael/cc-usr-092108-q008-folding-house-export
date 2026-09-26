"""海外服务站扫码入口：python -m src.scan SN-SYF20-002

打印该序列号的出厂快照、证书、关键决定链与异常责任，
不依赖任何网络或外部服务。
"""

import argparse
import json
from pathlib import Path

from .records import load_records
from .timeline import serial_dossier


def main() -> None:
    parser = argparse.ArgumentParser(description="按序列号重现制造到运维档案")
    parser.add_argument("serial_no")
    parser.add_argument("--records", default="fixtures/records.json")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 档案")
    args = parser.parse_args()

    book = load_records(Path(args.records))
    dossier = serial_dossier(book, args.serial_no)

    if args.json:
        print(json.dumps(dossier, ensure_ascii=False, indent=2))
        return

    b = dossier["as_built"]
    print(f"序列号 {dossier['serial_no']}｜{dossier['product']}｜目的国 {dossier['destination_country']}｜{dossier['service_station']}")
    print(f"  出厂配置 {b['config_id']} v{b['version']}")
    print(f"  电气制式 {b['electrical']['voltage']} {b['electrical']['frequency']}（{b['electrical']['standard']}）")
    print(f"  防风 {b['wind_rating']}｜抗震 {b['seismic_rating']}")
    print(f"  内装 {b['interior_package']}｜智能模块 {', '.join(b['smart_modules'])}")
    cert = dossier["certificate"]
    print(f"  证书 {cert['id']}（有效期至 {cert['valid_until']}，本站可访问：{cert['accessible_at_this_station']}）")
    print(f"  冻结链有效：{dossier['order']['frozen_chain_valid']}")

    if dossier["ncrs"]:
        print("  异常与责任：")
        for n in dossier["ncrs"]:
            closed = n.get("closed_on") or "未关闭"
            print(f"    - {n['id']} {n['disposition']} 责任={n['responsibility']} 关闭={closed}")
    if dossier["damages"]:
        for d in dossier["damages"]:
            print(f"    - {d['id']} 运输损伤 责任={d['determined_responsibility']} 索赔={d.get('claim_no')}")
    if dossier["warranty"]:
        w = dossier["warranty"]
        print(f"  保修自 {w['started_on']} 起 {w['months']} 个月")

    print("  决定链：")
    for d in dossier["decisions"]:
        print(f"    {d['ts']} [{d['stage']}] {d['summary']}（责任：{d['responsibility']}）")


if __name__ == "__main__":
    main()
