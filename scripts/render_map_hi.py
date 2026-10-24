# -*- coding: utf-8 -*-
"""渲染一张「独立高清全线地图」——把地图 SVG 按底图原生像素 1:1 出来。

为什么要单独出一张：
  攻略页里的地图受版式限制只能占 ~1161 设备像素宽，长图放大后必然糊。
  要给"放大看得清"，就得给一张**像素足够多**的独立地图：
  本脚本按自渲染底图的原生宽（make_terrain 的 RASTER_W，默认 3008）渲染。

⚠ 底图必须 embed_max=0（不降采样）才是真正的 1:1。
  HTML 版为了控体积把底图降到 1600 px 再内嵌；直接复用它渲染 3008 px，
  底图会被放大 1.88 倍、糊掉 —— 高清图的价值就没了。

⚠⚠ 无头浏览器的 `--window-size` **不等于视口**（窗口上还有一层浏览器 chrome，
   新版无头模式同样计），实测 `--window-size=3008,2018` 拿到的可视区只有
   2984×1928 —— 直接按 W,H 出图会在**右侧裁掉 24 px、底部裁掉 90 px**，
   而且裁掉的那块是页面的白底，和"留白"长得一模一样，肉眼只能看见"底部有点空"。
   → 正解：**开窗留余量 + 按量到的 SVG 矩形裁**（与 shoot_guide.py 同一套做法）。
"""
import time

import numpy as np
from PIL import Image

import guide_common as GC
import map_svg as MS
import route_def as RD

CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
OUT_JPG = GC.ROOT / f"{STEM}-全线地图.jpg"

_SS = max(2, int(CFG.get("map_ss", 2) or 2))
W = MS.IW * _SS                     # 与 make_terrain 的超采样一致（逻辑宽 × SS = 栅格宽）
# ⚠ 独立高清图**要带名称带**（map_svg.TITLE_H = 132 逻辑 px）：这张图是单独发出去的，
#   没有标题就只剩一片地形与几根彩色线，收到的人不知道这是哪条线、几天、多长。
#   HTML 内嵌版不加（页面已有 h1），所以这里显式 with_title=True，画布高也必须用
#   out_size(True) —— 两处不一致会变成"图被上下压缩 / 留白"（2026-10-21 珠峰东坡实测：
#   旧版 this file 只算 IH，标题带从来没打开过，交付物一直是光板地图）。
_IH = MS.out_size(True)[1]
H = round(_IH * W / MS.IW)
SLACK_W, SLACK_H = 60, 260          # 给浏览器 chrome 留的余量（只用于开窗，不参与裁剪）


def _page_html(svg_path_svg):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        '*{margin:0;padding:0}html,body{background:#fff}'
        f'svg{{display:block;width:{W}px;height:{H}px}}'
        f'</style></head><body>{svg_path_svg}</body></html>')


def _assert_full_frame(a, tag):
    """断言：没有整行/整列近乎纯白（白带 = 内容被裁，页面白底露出来了）。"""
    white = (a > 246).all(2)
    worst_row = white.mean(1).max()
    worst_col = white.mean(0).max()
    if worst_row > 0.6 or worst_col > 0.6:
        raise RuntimeError(f"{tag}：有整行/整列近乎全白（行 {worst_row:.0%} / 列 {worst_col:.0%}）"
                           f"—— 内容被裁了，别交付")
    print(f"  满幅校验：最白行 {worst_row:.0%} · 最白列 {worst_col:.0%}（均应 < 60%）")


def main():
    svg, _ = MS.build_svg(mobile=False, embed_image=True, embed_max=0, with_title=True)
    html = GC.ROOT / "_map_hi.html"
    html.write_text(_page_html(svg), encoding="utf-8")

    raw = GC.ROOT / "_map_hi.png"
    raw.unlink(missing_ok=True)
    try:
        t = time.time()
        # SVG 在 (0,0)、尺寸 W×H（body 白底、svg 无 margin）；开窗留余量，按 (0,0,W,H) 裁。
        # 不再用 --dump-dom（本机 Edge 不输出），几何裁切与旧版量到的偏移一致。
        im = GC.browser_shot(html, raw, W + SLACK_W, H + SLACK_H, scale=1)
        box = (0, 0, W, H)
        im = im.crop(box)
        if im.size != (W, H):
            raise RuntimeError(f"裁出来是 {im.size}，应为 {(W, H)}")
        _assert_full_frame(np.asarray(im).astype(np.int16), "高清地图")
        im.save(OUT_JPG, quality=88, optimize=True, progressive=True, subsampling=0)
        print(f"  {im.size} -> {OUT_JPG.name}  {OUT_JPG.stat().st_size/1048576:.2f} MB  "
              f"{time.time()-t:.1f}s")
    finally:
        raw.unlink(missing_ok=True)
        html.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
