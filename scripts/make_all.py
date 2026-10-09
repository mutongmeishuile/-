# -*- coding: utf-8 -*-
"""一键跑完整条流水线：轨迹 → OSM → 地形 → HTML →（长图 ∥ 高清地图 ∥ GPX）→ 自检。

为什么要有它
------------
实测下游单步都很快（出 HTML <1 s、一张长图 5–8 s、高清地图 5 s），
但每次改参数都要手敲 5 条命令 —— **慢的不是计算，是人的往返**。
有了这条命令，改起来才敢改（配合 --skip-dem 单轮只要 ~15 s）。

**只出宽屏长图**：本技能不再生成手机版长图（用户明确要求）；
手机阅读直接看 HTML —— 它本身是响应式的（<900px 切单栏）。
所以 `--split long=...` 那个用法已经没用了，别再照抄。

并行：默认开，但别指望它省多少
-----------------------------------
步骤之间是有依赖的，真正能同时跑的只有"无依赖的同一层"，本脚本按 DAG 分层并行：

    wave0:  prep                 （KML → out/track_*.json，后面所有步的窗口都靠它）
    wave1:  osm ∥ gpx            （窗口 = 轨迹包围盒；gpx 只读 KML 与 route_def）
    wave2:  dem                  （依赖 prep + osm）
    wave3:  html                 （依赖 dem + prep）
    wave4:  long ∥ map           （都只吃 HTML，两个浏览器进程可并存）
    wave5:  qa                   （依赖前两步产物）

实测省下的只有 ~6–10 s/轮。**真正的大头是 DEM 下载（首次 ~1–2 min），
它在 wave1 里是独苗，并行救不了它** —— 要压它得提 make_terrain.load_dem(workers=)
的并发度（默认已 24）并复用 demcache/。所以本脚本的价值主要是
「一条命令、人不用守着」，不是算力并行。

用法
----
    python scripts/make_all.py                  # 全跑（默认分层并行）
    python scripts/make_all.py --skip-dem       # 跳过 OSM+地形（改配色/改标注时用）
    python scripts/make_all.py --only html,long,map
    python scripts/make_all.py --serial         # 强制串行（并行输出看不清时排错用）
    python scripts/make_all.py --jobs 2         # 限制同时跑的进程数
    python scripts/make_all.py --dry-run        # 只打印执行计划与分层

步骤名：prep / osm / dem / html / long / map / gpx / qa
脚本按 "每步一个通用脚本" 约定自动发现（不再按线路复制脚本）：
    prep_track.py · fetch_osm.py · make_terrain.py · build_guide.py ·
    shoot_guide.py · render_map_hi.py · make_gpx.py · qa_guide.py
线路相关的**只有 route_def.py 一个文件**。
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
    "build_route_guide.py",  # 无轨迹时的手绘方案模板
    "make_all.py", "preflight.py", "guide_common.py",
}

# 依赖表：key -> 必须先完成的前置步骤
DEPS = {
    "prep": [],          # KML → out/track_*.json
    "osm":  ["prep"],    # 窗口由轨迹包围盒推 → 必须先有轨迹
    "gpx":  ["prep"],    # 要全量原始点
    "dem":  ["prep", "osm"],   # 等高线标注要避让轨迹与 POI
    "html": ["dem", "prep"],   # 要 base_map.jpg + profile_real.json
    "long": ["html"],
    "map":  ["html"],
    "qa":   ["long", "map"],
}

# 各步骤的默认附加参数。OSM 抓取走「软失败」：Overpass 集体抽风时
# 只丢一层矢量，不该让整条流水线报错（地形晕渲与等高线照常出）。
STEP_ARGS = {"osm": ["--soft"]}


def _pick(prefixes, suffix=".py"):
    hits = sorted(p.name for p in HERE.glob(f"*{suffix}")
                  if p.name not in EXCLUDE and any(p.name.startswith(x) for x in prefixes))
    return hits


def discover():
    """按"每步一个通用脚本"约定自动发现；找不到就留空（跑时跳过并提醒）。"""
    return [
        ("prep", "轨迹准备", _pick(["prep_track"])),
        ("osm",  "抓 OSM",   _pick(["fetch_osm"])),
        ("dem",  "地形底图", _pick(["make_terrain"])),
        ("html", "出 HTML",  _pick(["build_guide"])),
        ("long", "长图 PNG", _pick(["shoot_guide"])),
        ("map",  "高清地图", _pick(["render_map_hi"])),
        ("gpx",  "GPX 导出", _pick(["make_gpx"])),
        ("qa",   "自检",     _pick(["qa_guide"])),
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
                    help="把某步骤按参数拆成并行子任务，如 long=a,b（本项目已无手机版长图，"
                         "一般用不到；仅对'同脚本、不同参数、产物不重名'的步骤安全）")
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
                    jobs.append((k, s, tuple(STEP_ARGS.get(k, ()))))
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
