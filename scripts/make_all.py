# -*- coding: utf-8 -*-
"""一键跑完整条流水线：OSM → 地形 → HTML →（长图 ∥ 高清地图 ∥ GPX）→ 自检。

为什么要有它
------------
实测下游单步都很快（出 HTML <1 s、一张长图 7 s、高清地图 5 s），
但每次改参数都要手敲 4–5 条命令 —— **慢的不是计算，是人的往返**。
有了这条命令，改起来才敢改（配合 --skip-dem 单轮只要 ~15 s）。

并行：默认开，**但别指望它省多少**
-----------------------------------
步骤之间是有依赖的（OSM → 地形 → HTML → 长图/地图 → 自检），
真正能同时跑的只有「无依赖的同一层」，本脚本按 DAG 分层并行：

    wave0:  osm ∥ gpx            （都只读外部数据，互不干涉）
    wave1:  dem                  （依赖 osm.json）
    wave2:  html                 （依赖 base_map.jpg）
    wave3:  long ∥ map           （都只吃 HTML，两个 Chrome 进程可并存）
    wave4:  qa                   （依赖前两步产物）

实测省下的只有 ~6–10 s/轮（long 14s + map 5s 串行 → 并行 14s）。
**真正的大头是 DEM 下载（~8 min，一次性），它在 wave1 里是独苗，并行救不了它**
——要压它得提 make_terrain.load_dem(workers=10) 的并发度，见 references/efficiency.md §3.7。
所以本脚本的价值主要是「一条命令、人不用守着」，不是算力并行。

用法
----
    python scripts/make_all.py                  # 全跑（默认分层并行）
    python scripts/make_all.py --skip-dem       # 跳过 OSM+地形（改配色/改标注时用，省 2 min+）
    python scripts/make_all.py --only long,map  # 只跑某几步
    python scripts/make_all.py --serial         # 强制串行（并行输出看不清时排错用）
    python scripts/make_all.py --jobs 2         # 限制同时跑的进程数
    python scripts/make_all.py --split long=desk,phone  # 长图两版拆成两个进程并行
    python scripts/make_all.py --dry-run        # 只打印执行计划与分层

`--split` 只对「同脚本、不同参数、产物互不重名」的步骤安全（长图 desk/phone 正是如此）；
脚本不认参数时别加 —— 多余 argv 会被静默忽略，白拆一场。

步骤名：osm / dem / html / long / map / gpx / qa
脚本名按约定自动发现（线路相关脚本保持 <verb>_<线路>.py 命名即可）；
发现结果会先打印出来，认错了就用 --only + 显式脚本名，或改 STEPS/DEPS。
"""
import argparse
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
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

# 依赖表：key -> 必须先完成的前置步骤（改线路时按实际依赖调整）
DEPS = {
    "osm":  [],
    "gpx":  [],          # 只读 track_*.json / route_def，跟底图完全无关
    "dem":  ["osm"],     # make_terrain 要叠 out/osm.json
    "html": ["dem"],     # 要读 out/base_map.jpg
    "long": ["html"],
    "map":  ["html"],
    "qa":   ["long", "map"],
}


def _pick(prefixes, suffix=".py"):
    hits = sorted(p.name for p in HERE.glob(f"*{suffix}")
                  if p.name not in EXCLUDE and any(p.name.startswith(x) for x in prefixes))
    return hits


def discover():
    """按命名约定自动发现各步骤脚本；找不到就留空（跑时跳过并提醒）。"""
    return [
        ("osm",  "抓 OSM",   _pick(["fetch_osm"])),
        ("dem",  "地形底图", _pick(["make_terrain"])),
        ("html", "出 HTML",  [n for n in _pick(["build_"])
                              if "base_map" not in n and "route_guide" not in n]),
        ("long", "长图 PNG", _pick(["shoot_"])),
        ("map",  "高清地图", _pick(["render_"], "_hi.py") or _pick(["render_"])),
        ("gpx",  "GPX 导出", _pick(["make_gpx"])),
        ("qa",   "自检",     _pick(["qa_"])),
    ]


def layers(plan_keys):
    """按 DEPS 做拓扑分层：同一层内的步骤互不依赖，可以并行。"""
    done, out = set(), []
    left = [k for k in plan_keys]
    while left:
        wave = [k for k in left if all(d in done or d not in plan_keys for d in DEPS.get(k, []))]
        if not wave:                      # 依赖成环或配置写错，兜底全塞一层
            wave, left = left, []
        out.append(wave)
        done |= set(wave)
        left = [k for k in left if k not in done]
    return out


def run_one(name, script, args=()):
    """跑一个脚本：输出进临时文件，避免多进程共享 pipe 时缓冲区打满互相阻塞。"""
    t = time.time()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as fh:
        r = subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(HERE),
                           stdout=fh, stderr=subprocess.STDOUT)
        fh.seek(0)
        log = fh.read()
    dt = time.time() - t
    tail = "\n".join(log.strip().splitlines()[-8:])
    return r.returncode, script, dt, tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dem", action="store_true",
                    help="跳过 OSM 抓取与地形渲染（改配色/改标注时用，省 2 min+）")
    ap.add_argument("--only", default="", help="只跑指定步骤，逗号分隔，如 html,long")
    ap.add_argument("--serial", action="store_true", help="强制串行（排错时用）")
    ap.add_argument("--jobs", type=int, default=0, help="同时跑的进程数上限（默认不限）")
    ap.add_argument("--split", default="",
                    help="把某步骤按参数拆成并行子任务，如 long=desk,phone（多组用 ; 分隔）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # ⚠ 逗号是「同一组内多个参数」，分号才是「多组」——
    #   写成 group=a,b 时绝不能把 b 当成新组，否则 phone 那一版会被静默漏掉。
    split, cur = {}, None
    for item in a.split.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, _, v = item.partition("=")
            cur = k.strip()
            split[cur] = [v.strip()] if v.strip() else []
        elif cur:
            split[cur].append(item)

    found = dict((k, (d, s)) for k, d, s in discover())
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    if only:
        unknown = only - set(found)
        if unknown:
            sys.exit(f"未知步骤：{unknown}（可选：{list(found)}）")

    plan = []
    for key, (desc, scripts) in found.items():
        if only and key not in only:
            continue
        if key in ("osm", "dem") and a.skip_dem:
            continue
        if not scripts:
            print(f"  [..]   {desc}({key})：没找到脚本，跳过")
            continue
        plan.append((key, desc, scripts))

    waves = layers([k for k, _, _ in plan])
    print("执行计划（同一 wave 内并行）：")
    for i, wave in enumerate(waves):
        items = ", ".join(f"{k}[{'+'.join(dict((x,y) for x,_,y in plan)[k])}]"
                          for k in wave if k in dict((x, y) for x, _, y in plan))
        print(f"  wave{i}: {items}")

    by_key = {k: (desc, scripts) for k, desc, scripts in plan}
    print("\n脚本发现结果：")
    for key, (desc, scripts) in by_key.items():
        print(f"  {key:5s} {desc:8s} <- {', '.join(scripts)}")
    if a.dry_run:
        return 0

    fail, t_all = 0, time.time()
    for i, wave in enumerate(waves):
        jobs = []
        for k in wave:
            if k not in by_key:
                continue
            for s in by_key[k][1]:
                if k in split:                      # 同脚本多参数 → 拆成独立并行子任务
                    jobs.extend((k, s, (arg,)) for arg in split[k])
                else:
                    jobs.append((k, s, ()))
        if not jobs:
            continue
        parallel = (not a.serial) and len(jobs) > 1
        label = ", ".join(f"{s} {' '.join(args)}".strip() for _, s, args in jobs)
        print(f"\n=== wave{i}（{'并行' if parallel else '串行'}）{label} ===")
        t = time.time()
        if parallel:
            n = a.jobs if a.jobs > 0 else len(jobs)
            with ThreadPoolExecutor(max_workers=n) as ex:
                results = list(ex.map(lambda j: run_one(j[1], j[1], j[2]), jobs))
        else:
            results = [run_one(s, s, args) for _, s, args in jobs]
        for rc, script, dt, tail in results:
            if rc:
                print(f"  !! {script} 退出码 {rc}（{dt:.1f}s）\n{tail}")
                fail += 1
            else:
                print(f"  ✓ {script} 用时 {dt:.1f}s")
        print(f"  wave{i} 合计 {time.time() - t:.1f}s")

    print(f"\n总耗时 {time.time() - t_all:.1f}s — " + ("全流程完成 ✓" if not fail else f"{fail} 步失败 ↑"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
