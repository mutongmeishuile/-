# -*- coding: utf-8 -*-
"""清掉中间产物，只留交付物（攻略 HTML / 长图 / 全线地图 / GPX / 原始 KML）。

为什么要有它
------------
一条线路定稿后，根目录会剩下 `_qa_desk.png`、`_qa_phone.png`（各 1–2 MB 的审计
截图）、`out/osm.json.bak`、`out/base_terrain.jpg` 之类的东西。它们**只是跑图过程
留下的**，却和交付物混在一起 —— 用户每次都要手动挑出来删。

**安全原则（改这个文件前先读）**
1. 只删白名单里的文件名模式，绝不递归删目录、绝不 glob 通配到未知文件；
2. 动手前先算「保留集」（交付物 + 原始输入），任何命中保留集的文件都会被拒删并报警；
3. 每个删掉的文件都打印出来，且先算总释放空间 —— 用户永远知道自己少了什么；
4. 只在本线路根目录（`GC.ROOT`）与 `out/` 两层里操作，路径不在这两处一律不碰。

用法
----
    python scripts/clean.py            # 清临时件（QA 截图 / 备份 / 可再生的中间图）
    python scripts/clean.py --dry-run   # 只列出要删的，不真删（拿不准时先跑这个）
    python scripts/clean.py --keep-qa   # 保留 _qa_*.png 审计截图（要目视核对时用）
    python scripts/clean.py --deep      # 连 out/ 的缓存一起清（OSM/DEM 元数据、轨迹 json）
                                        #   ⚠ 之后再出图会重新下载 DEM 与 OSM（数分钟）

`make_all.py` 定稿跑完会自动调用本脚本（`--keep-temp` 可关掉）。
"""
import argparse
import fnmatch
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guide_common as GC          # noqa: E402


# ---------------------------------------------------------------- 白名单
# 形态：(相对 GC.ROOT 的目录, 文件名 glob, 说明)
# 只按文件名模式匹配，**不递归**；目录不存在就跳过。
TEMP_SPECS = [
    ("",        "_qa_*.png",            "QA 版式审计截图"),
    ("",        "_qa_*.html",           "QA 审计用的注入页"),
    ("",        "_shoot_raw.png",       "长图截图的原始拼块"),
    ("",        "*.bak",                "手工备份"),
    ("",        "*.bak_*",              "手工备份（带后缀）"),
    ("",        "*-攻略长图_raw.png",    "长图中间产物"),
    ("out",     "base_terrain.jpg",     "地形栅格（未叠 OSM 的中间版，只写不读）"),
    ("out",     "research_brief.md",    "调研提纲（一次性回填清单）"),
    ("out",     "kml_pois.json",        "KML 具名标注（只在 new_route 阶段用）"),
    ("out",     "*.bak",                "out 下的备份"),
    ("out",     "*.bak_*",              "out 下的备份（带后缀）"),
]

# --deep 追加：这些是**可再生的缓存**，删了要重新下载/解析
DEEP_SPECS = [
    ("out",     "osm.json",             "OSM 要素（重抓约 1–3 min）"),
    ("out",     "osm_draw.json",        "OSM 绘制裁剪版"),
    ("out",     "label_boxes.json",     "底图地名锚点"),
    ("out",     "base_map.jpg",         "地形底图（重渲约 1 min）"),
    ("out",     "base_meta.json",       "底图元数据"),
    ("out",     "track_full.json",      "全量轨迹（重解析 KML 即得）"),
    ("out",     "track_real.json",      "去抽稀轨迹"),
    ("out",     "profile_real.json",    "剖面数据"),
]



def _deliverables(root):
    """交付物 = 根目录下这些后缀，且**不以 `_` 开头**（`_qa_*` 是审计截图不是交付物）。"""
    out = set()
    for p in root.iterdir():
        if not p.is_file() or p.name.startswith("_"):
            continue
        if p.suffix.lower() in (".html", ".png", ".jpg", ".jpeg", ".gpx", ".kml", ".md"):
            out.add(p.resolve())
    return out


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def plan(specs, root, protect):
    """把白名单展开成待删清单；命中保留集的一律剔除并打印警告。"""
    hits, guarded = [], []
    for sub, pat, why in specs:
        # "out" 是**脚本目录下的 out**（`GC.OUT` = scripts/out），不是 ROOT/out ——
        # 这里踩过一次：写成 root/"out" 会静默匹配不到任何文件、还报"没有需要清理"。
        d = GC.OUT if sub == "out" else (root / sub if sub else root)
        if not d.is_dir():
            continue
        for p in sorted(d.glob(pat)):
            if not p.is_file():
                continue
            if p.resolve() in protect:
                guarded.append((p, why))
                continue
            hits.append((p, why))
    return hits, guarded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列出，不删")
    ap.add_argument("--deep", action="store_true", help="连可再生的缓存一起清")
    ap.add_argument("--keep-qa", action="store_true", help="保留 _qa_* 审计截图")
    a = ap.parse_args()

    root = GC.ROOT
    specs = list(TEMP_SPECS) + (DEEP_SPECS if a.deep else [])
    if a.keep_qa:
        specs = [s for s in specs if not s[1].startswith("_qa_")]

    protect = _deliverables(root)
    hits, guarded = plan(specs, root, protect)

    if not hits:
        print("没有需要清理的中间产物 ✓")
        if guarded:
            _report_guarded(guarded)
        return 0

    total = sum(p.stat().st_size for p, _ in hits)
    print(f"{'将清理' if a.dry_run else '正在清理'} {len(hits)} 个中间产物，"
          f"共 {_human(total)}：")
    for p, why in hits:
        rel = p.relative_to(root).as_posix()
        print(f"  - {rel:36s} {_human(p.stat().st_size):>9s}   {why}")

    if a.dry_run:
        print("\n（--dry-run：未删除）")
        if guarded:
            _report_guarded(guarded)
        return 0

    failed = []
    for p, _ in hits:
        try:
            p.unlink()
        except OSError as e:                        # 被占用（如截图还在写）就先跳过
            failed.append((p, e))
    print(f"\n已删除 {len(hits) - len(failed)} 个，释放 {_human(total)}。")
    for p, e in failed:
        print(f"  ! 删不掉 {p.relative_to(root).as_posix()}：{e}")
    if guarded:
        _report_guarded(guarded)
    print("保留：攻略 HTML / 长图 PNG / 全线地图 JPG / GPX / 原始 KML"
          + ("（--deep 已清 out/ 缓存，下次出图会重新下载 DEM 与 OSM）" if a.deep else ""))
    return 1 if failed else 0


def _report_guarded(guarded):
    print("\n⚠ 以下文件命中白名单但属于交付物，已拒绝删除（白名单写错了）：")
    for p, why in guarded:
        print(f"  ! {p}（{why}）")


if __name__ == "__main__":
    sys.exit(main())
