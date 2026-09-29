# -*- coding: utf-8 -*-
"""一键跑完整条流水线：地形 → HTML → 长图 → 高清地图 → GPX → 自检。

为什么要有它
------------
实测下游单步都很快（出 HTML <1 s、一张长图 7 s、高清地图 5 s），
但每次改参数都要手敲 4–5 条命令 —— **慢的不是计算，是人的往返**。
有了这条命令，改起来才敢改（配合 --skip-dem 单轮只要 ~15 s）。

用法
----
    python scripts/make_all.py                  # 全跑
    python scripts/make_all.py --skip-dem       # 跳过地形（改配色/改标注时用，省 2 min）
    python scripts/make_all.py --only html,long # 只跑某几步
    python scripts/make_all.py --dry-run        # 只打印将要执行的步骤

步骤名：dem / html / long / map / gpx / qa
脚本名按约定自动发现（线路相关脚本保持 <verb>_<线路>.py 命名即可）；
发现结果会先打印出来，认错了就用 --only + 显式脚本名，或改 STEPS。
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 排除掉"模板/其他路线方案"的脚本，避免被自动发现误挑
EXCLUDE = {
    "build_base_map.py",     # B 路线（在线瓦片）专用，不在默认链里
    "build_route_guide.py",  # 手绘方案模板
    "shoot_long_png.py",     # 长图模板
    "render_map_hi.py",      # 高清地图模板
    "make_all.py", "preflight.py",
}


def _pick(prefixes, suffix=".py"):
    hits = sorted(p.name for p in HERE.glob(f"*{suffix}")
                  if p.name not in EXCLUDE and any(p.name.startswith(x) for x in prefixes))
    return hits


def discover():
    """按命名约定自动发现各步骤脚本；找不到就留空（跑时跳过并提醒）。"""
    return [
        ("dem",  "地形底图", _pick(["make_terrain"])),
        ("html", "出 HTML", [n for n in _pick(["build_"]) if "base_map" not in n and "route_guide" not in n]),
        ("long", "长图 PNG", _pick(["shoot_"])),
        ("map",  "高清地图", _pick(["render_"], "_hi.py") or _pick(["render_"])),
        ("gpx",  "GPX 导出", _pick(["make_gpx"])),
        ("qa",   "自检",     _pick(["qa_"])),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dem", action="store_true", help="跳过地形渲染（改配色/改标注时用）")
    ap.add_argument("--only", default="", help="只跑指定步骤，逗号分隔，如 html,long")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    steps = discover()
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    if only:
        unknown = only - {k for k, _, _ in steps}
        if unknown:
            sys.exit(f"未知步骤：{unknown}（可选：{[k for k,_,_ in steps]}）")
    plan = []
    for key, desc, scripts in steps:
        if only and key not in only:
            continue
        if key == "dem" and a.skip_dem:
            continue
        if not scripts:
            print(f"  [..]   {desc}({key})：没找到脚本，跳过")
            continue
        plan.append((key, desc, scripts))

    print("执行计划：")
    for key, desc, scripts in plan:
        print(f"  {key:5s} {desc:8s} <- {', '.join(scripts)}")
    if a.dry_run:
        return 0

    import time
    fail = 0
    for key, desc, scripts in plan:
        for s in scripts:
            t = time.time()
            print(f"\n=== [{key}] {s} ===")
            r = subprocess.run([sys.executable, str(HERE / s)], cwd=str(HERE))
            dt = time.time() - t
            if r.returncode:
                print(f"  !! {s} 退出码 {r.returncode}")
                fail += 1
            else:
                print(f"  ✓ {s} 用时 {dt:.1f}s")
    print("\n" + ("全流程完成 ✓" if not fail else f"{fail} 步失败 ↑"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
