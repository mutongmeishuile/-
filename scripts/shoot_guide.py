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
PAD_BOTTOM = 48                 # 成图底部留白（与版心同色，观感更稳）

# ⚠ 超长页面会被无头浏览器**静默截白**：2026-10-24 库拉岗日实测，页面高约 8800 CSS
#   （×2 = 17600 设备 px），内容只渲染到 ~13800 设备行就整幅变白；而"内容没顶到画布底"
#   的旧判据把截白处当成了页底 —— 交付的长图直接缺了最后三分之一（时间表/提示/脚注全没）。
#   → 与 render_map_hi 同一套解法：**分带截图**（body 相对定位上移 + 逐带裁剪拼接），
#     每带 ≤ 12000 设备 px（实测 13700+ 处开始截白，留足余量），底边探测只在带内做。
BAND = 6000                     # 每带设备像素高（SCALE=2 → 3000 CSS）。实测截白从窗口顶
                                # ~13700 行起（窗口相关），6000 行留足余量
OVERLAP = 400                   # 带间重叠（设备 px）：窗口底部总有 ~100–200 行"未画尾巴"，
                                # 后一带的顶部（画好了）覆盖前一带的尾巴，消掉接缝白条
MAX_BANDS = 12
TAIL_ROWS = 400                 # 页底判定：带内内容底边距带底 ≥ 400 行才算真页底


def _band_html(html_src, offset_px):
    """给页面注入 body 相对定位偏移（负值上移），供第 offset_px 设备行所在带截图。

    ⚠ CSS `top` 单位是 **CSS px**，offset 是设备 px —— 必须除以 SCALE，
    否则带与带之间会跳过 SCALE×offset 的内容（2026-10-24 实测踩过）。
    """
    inject = f"<style>body{{position:relative;top:{-offset_px // SCALE}px}}</style></head>"
    return html_src.replace("</head>", inject, 1)


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


def _rm_quiet(p: Path):
    """清理临时文件是尽力而为：环境层可能拦截大文件删除（SAFE_DELETE 钩子），
    删不掉就留着并提示，绝不能让整条出图流水线因此 rc!=1 前功尽弃。"""
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        print(f"  ⚠ 临时文件 {p.name} 清理失败（不影响成图，可手动删）: {e}")


def render():
    raw = GC.ROOT / "_shoot_raw.png"
    band_html = GC.ROOT / "_shoot_band.html"
    html_src = HTML.read_text(encoding="utf-8")
    win_h = BAND // SCALE + 80       # 每带开窗高（CSS px），略高于带高
    _rm_quiet(raw)
    x0 = (WIN_W - DESIGN_W) // 2 * SCALE
    x1 = x0 + DESIGN_W * SCALE
    t = time.time()

    # ① 逐带截图：body 相对定位上移 offset，每带取 (x0,0)-(x1,BAND)；
    #    带内出现"页底留白"（内容底边距画布底 > 8 行）即认为找到真页底。
    #    ⚠ 不能只靠"没顶到底 = 页底"：截白会把内容底边往前挪，必须每带显式验证。
    parts, page_bottom, _BODY_BG = [], None, None
    for bi in range(MAX_BANDS):
        off = bi * (BAND - OVERLAP)
        band_html.write_text(_band_html(html_src, off), encoding="utf-8")
        _rm_quiet(raw)
        im = GC.browser_shot(band_html, raw, WIN_W, win_h, SCALE)
        bg = im.getpixel((2, 2)) if _BODY_BG is None else _BODY_BG
        _BODY_BG = bg
        a = np.asarray(im.convert("RGB")).astype(np.int16)
        off_mask = np.abs(a - np.asarray(bg, np.int16)).sum(axis=2) > 14
        rows = np.where(off_mask.sum(axis=1) > 0.02 * a.shape[1])[0]
        band_h = min(BAND, im.height)
        if not rows.size:
            page_bottom = off                          # 整带都是页底留白 → 上一带已是页底
        elif int(rows[-1]) + 1 < band_h - TAIL_ROWS:
            page_bottom = off + int(rows[-1]) + 1     # 真页底（带内有足量留白）
        parts.append(im.crop((x0, 0, x1, band_h)))
        if page_bottom is not None:
            break
    else:
        raise RuntimeError(f"超过 {MAX_BANDS} 带仍未找到页底 —— 页面异常，请检查 HTML")
    _rm_quiet(band_html)
    _rm_quiet(raw)
    print(f"  分带 {bi+1} 段（步进 {BAND-OVERLAP}）· 页底 @ {page_bottom}px · "
          f"截图用时 {time.time()-t:.1f}s")

    # ② 按绝对偏移拼接：后一带覆盖前一带底部可能未画的尾巴（OVERLAP 起作用）
    total_h = page_bottom + PAD_BOTTOM * SCALE
    out_im = Image.new("RGB", (x1 - x0, total_h), _BODY_BG)
    for k, p in enumerate(parts):
        out_im.paste(p, (0, k * (BAND - OVERLAP)))
    out_im.save(OUT_PNG, optimize=True)
    out = Image.open(OUT_PNG)
    print(f"  -> {out.size} · {OUT_PNG.stat().st_size/1048576:.2f} MB · {OUT_PNG.name}")


if __name__ == "__main__":
    if not HTML.exists():
        sys.exit(f"缺 {HTML} —— 先跑 build_guide.py")
    render()
