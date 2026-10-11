# -*- coding: utf-8 -*-
"""把攻略 HTML 渲染成分享长图（**只出宽屏版**）。

⚠ 本技能不再生成手机版长图（用户明确要求）。
   手机上请直接看 HTML —— 它本身是响应式的（<900px 切单栏）。
   长图只保留宽屏版：微信里点开看大图 / 存档 / 打印都够用。

提速（相对旧版 3 次全尺寸截图）
-------------------------------
旧流程：按经验填一个很大的窗高 → 截图 → 量到内容顶到底 → ×1.35 重截 → 再量……
一次长图要 2–3 次**全尺寸**光栅化（2344×10800 ≈ 2500 万像素 ×3），是最慢的一步。

新流程：**先用 DOM 量，再截一次**
  ① `--dump-dom` 注入 JS 量 `.page` 的实际矩形与文档高度 —— 只排版不光栅，约 0.5 s；
  ② 按量到的尺寸**恰好**开窗截一次，不再靠"猜 + 重试"。
  ③ 截完再用像素扫一遍做断言（内容底边必须离画布底 >2 px），
     因为 DOM 量和光栅化偶有 1–2 px 出入，宁可报错也不要静默截断。

⚠ 另一个老坑（保留）：**不要用 --window-size 反推裁剪区间**。
  无头浏览器对窗口宽有最小钳制，`.page` 会在其中居中，直接按窗宽裁会切掉右侧整条、
  表现为"每行末尾缺字"。所以一律**按量到的版心边界**裁，并断言宽度 == 设计宽 × 倍率。
"""
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

import guide_common as GC
import route_def as RD

CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
HTML = GC.ROOT / f"{STEM}-攻略.html"
OUT_PNG = GC.ROOT / f"{STEM}-攻略长图.png"
DESIGN_W = 1120                 # .page 版心宽（CSS px）
SCALE = 2
WIN_W = 1200                    # 比版心宽即可（.page 会居中），不参与裁剪
WIN_H_TRY = 7000                # 开窗高度（CSS px），需 > 内容高；不够会重开大一档
PAD_BOTTOM = 48                 # 成图底部留白（与版心同色，观感更稳）


def _page_box_from_pixels(im, bg):
    """从像素量版心（.page 底色与 body 底色不同，可分辨）。"""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    off = np.abs(a - np.asarray(bg, np.int16)).sum(axis=2) > 14
    rows = np.where(off.sum(axis=1) > 0.02 * a.shape[1])[0]
    if not rows.size:
        return None
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    cols = np.where(off[y0:y1].sum(axis=0) > 0.5 * (y1 - y0))[0]
    if not cols.size:
        return None
    return int(cols[0]), int(cols[-1]) + 1, y0, y1


def render():
    raw = GC.ROOT / "_shoot_raw.png"
    raw.unlink(missing_ok=True)
    # ① 直接截整页（窗口给足高）；不再用 --dump-dom（本机 Edge 不输出）
    win_h = WIN_H_TRY
    for attempt in range(4):
        t = time.time()
        im = GC.browser_shot(HTML, raw, WIN_W, win_h, SCALE)
        pg = _page_box_from_pixels(im, im.getpixel((2, 2)))
        if pg is None:
            raise RuntimeError("像素量不到版心（页面背景与 body 同色？）")
        x0, x1, y0, y1 = pg
        if y1 < im.height - 8:            # 内容没顶到画布底 → 高度够，收工
            break
        win_h = int(win_h * 1.4) + 200     # 顶到了 → 加高重截
        print(f"  …内容顶到画布底（{y1}/{im.height}），开窗加高到 {win_h} 重试")
    else:
        raise RuntimeError(f"内容仍顶到画布底（{y1}/{im.height}）—— 长图异常，请检查 HTML")

    want = DESIGN_W * SCALE
    if abs((x1 - x0) - want) > 4:
        print(f"  !! 量到版心宽 {x1-x0} px，应为 {want} px（CSS .page 被改过？）")
    # 长图从纸面顶开始，x 锁定为几何值更稳（.page 居中，左右各 40 CSS × 倍率）
    x0 = (WIN_W - DESIGN_W) // 2 * SCALE
    x1 = x0 + DESIGN_W * SCALE
    print(f"  截图 {im.size} · 版心 x {x0}–{x1}（宽 {want}）· 内容高 {y1} · 用时 {time.time()-t:.1f}s")

    im.crop((x0, 0, x1, y1 + PAD_BOTTOM * SCALE)).save(OUT_PNG, optimize=True)
    raw.unlink(missing_ok=True)
    out = Image.open(OUT_PNG)
    print(f"  -> {out.size} · {OUT_PNG.stat().st_size/1048576:.2f} MB · {OUT_PNG.name}")


if __name__ == "__main__":
    if not HTML.exists():
        sys.exit(f"缺 {HTML} —— 先跑 build_guide.py")
    render()
