# -*- coding: utf-8 -*-
"""开工预检：30 秒内把所有"环境约定"一次性确认完。

为什么要先跑这个
----------------
实测一次完整攻略 45 min，其中 5 项是纯环境/低级错误，全部在跑到一半时才暴露：
  · 自带 Python 没 numpy/Pillow → 跑到第 5 步才 ModuleNotFoundError，换解释器重来
  · 没装 Chrome 只有 Edge → 渲染脚本里的浏览器路径要改
  · out/ 与 scripts/out/ 两套路径约定 → 文件找不到
  · KML 格式判断错 → 解析出 0 个点，白跑一遍
这些都不是"设计错了"，是"约定没先确认"。花 30 秒跑完本脚本，能省下后面十几分钟。

用法
----
    python scripts/preflight.py [可选：KML 路径]

不传 KML 就只查环境；传了就顺带判断 KML 格式（gx:Track / LineString）该用哪个解析脚本。
"""
import importlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ok(msg):
    print(f"  [OK]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")
    return 1


def main():
    fail = 0
    print("① Python 与依赖")
    print(f"  解释器 {sys.executable}")
    for m, need in (("numpy", "地形栅格运算"), ("PIL", "图像读写")):
        try:
            importlib.import_module(m)
            ok(f"{m} 可用（{need}）")
        except ImportError:
            fail += bad(f"缺 {m} —— {need} 会崩。"
                        f"换有 numpy/Pillow 的那个解释器，或装：pip install {m if m=='numpy' else 'Pillow'}")

    print("② 无头浏览器（渲染长图必需）")
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    found = [p for p in cands if Path(p).exists()]
    if found:
        ok(f"找到 {found[0]}")
        if "Edge" in found[0]:
            print("         （Edge 需 --headless=new，Chrome 用 --headless）")
    else:
        fail += bad("没找到 Chrome/Edge —— 长图渲染无从谈起")

    print("③ 路径约定")
    out = HERE / "out"
    if out.is_dir():
        ok(f"scripts/out/ 存在（{len(list(out.glob('*.json')))} 个 json）")
    else:
        print("  [..]   scripts/out/ 不存在（首次运行正常，make_terrain 会建）")
    for f in ("make_terrain.py", "fetch_osm.py"):
        if not (HERE / f).is_dir() and (HERE / f).exists():
            ok(f"{f} 就位")
        else:
            fail += bad(f"缺 {f}")
    # ⚠ 两套 out/ 是本次真实返工项：有的脚本写 HERE/"out"，有的写上一级
    if (HERE.parent / "out").is_dir():
        print("  [warn] 上一级也有 out/ —— 确认所有脚本统一用 HERE/'out'，别两套并存")

    print("④ KML 格式（决定用哪个解析脚本，选错会得到 0 个点）")
    kml = sys.argv[1] if len(sys.argv) > 1 else None
    if not kml:
        print("  [..]   没传 KML，跳过（用法：python preflight.py 路径.kml）")
    else:
        p = Path(kml)
        if not p.exists():
            fail += bad(f"KML 不存在：{p}")
        else:
            s = p.read_text(encoding="utf-8", errors="ignore")
            n_coord = len(re.findall(r"<gx:coord>", s))
            n_when = len(re.findall(r"<when>", s))
            n_ls = len(re.findall(r"<LineString>", s))
            print(f"        gx:coord {n_coord} | when {n_when} | LineString {n_ls}")
            if n_coord:
                ok("→ gx:Track 格式，用 parse_track_kml.py")
            elif n_ls:
                ok(f"→ 分段 LineString 格式，用 parse_kml_ls.py（{n_ls} 段）")
            else:
                fail += bad("两种都不是 —— 先人工看一眼文件结构")
            if n_when > n_coord:
                print(f"        ⚠ when({n_when}) 多于 coord({n_coord})：多出的属于 POI 标注，"
                      f"解析时只取 <gx:Track> 块内的，别全文 findall")

    print("\n" + ("预检通过，可以开工 ✓" if not fail else f"有 {fail} 项没过，先修再往下走 ↑"))
    return fail


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
