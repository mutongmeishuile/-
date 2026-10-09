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
import json
import re
import subprocess
import time

import numpy as np
from PIL import Image

import guide_common as GC
import map_svg as MS
import route_def as RD

CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
OUT_JPG = GC.ROOT / f"{STEM}-全线地图.jpg"
BROWSER = GC.find_browser()

W = MS.IW * 2                       # 与 make_terrain 的超采样一致（逻辑宽 ×2 = 栅格宽）
H = round(MS.IH * W / MS.IW)
SLACK_W, SLACK_H = 60, 260          # 给浏览器 chrome / 滚动条留的余量（只用于开窗，不参与裁剪）

MEASURE_JS = r"""
<script>
window.addEventListener('load', function(){
  var s = document.querySelector('svg');
  var r = s.getBoundingClientRect();
  document.body.setAttribute('data-m', JSON.stringify({
    x: Math.round(r.left), y: Math.round(r.top),
    w: Math.round(r.width), h: Math.round(r.height),
    vpW: document.documentElement.clientWidth,
    vpH: document.documentElement.clientHeight
  }));
});
</script>
"""


def _page_html(svg_path_svg):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        '*{margin:0;padding:0}html,body{background:#fff}'
        f'svg{{display:block;width:{W}px;height:{H}px}}'
        f'</style></head><body>{svg_path_svg}{MEASURE_JS}</body></html>')


def _run(extra, html, timeout=600):
    return subprocess.run([BROWSER, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                           "--no-sandbox", *extra, html.as_uri()],
                          check=True, capture_output=True, timeout=timeout)


def _measure(html):
    """① DOM 量 SVG 矩形：只排版不光栅，约 0.5 s。"""
    dom = _run([f"--window-size={W + SLACK_W},{H + SLACK_H}", "--dump-dom"],
               html).stdout.decode("utf-8", "ignore")
    m = re.search(r'data-m="([^"]+)"', dom)
    if not m:
        raise RuntimeError("量尺脚本没跑起来（--dump-dom 里找不到 data-m）")
    import html as _h
    d = json.loads(_h.unescape(m.group(1)))
    if d["w"] < W or d["h"] < H:
        raise RuntimeError(f"SVG 被视口压扁了：量到 {d['w']}×{d['h']}，应为 {W}×{H}；"
                           f"视口 {d['vpW']}×{d['vpH']} —— 把 SLACK_W/SLACK_H 调大")
    return d


def _assert_full_frame(a, tag):
    """② 断言：没有整行/整列近乎纯白（白带 = 内容被裁，页面白底露出来了）。"""
    white = (a > 246).all(2)
    worst_row = white.mean(1).max()
    worst_col = white.mean(0).max()
    if worst_row > 0.6 or worst_col > 0.6:
        raise RuntimeError(f"{tag}：有整行/整列近乎全白（行 {worst_row:.0%} / 列 {worst_col:.0%}）"
                           f"—— 内容被裁了，别交付")
    print(f"  满幅校验：最白行 {worst_row:.0%} · 最白列 {worst_col:.0%}（均应 < 60%）")


def main():
    svg, _ = MS.build_svg(mobile=False, embed_image=True, embed_max=0)
    html = GC.ROOT / "_map_hi.html"
    html.write_text(_page_html(svg), encoding="utf-8")

    raw = GC.ROOT / "_map_hi.png"
    raw.unlink(missing_ok=True)
    try:
        t = time.time()
        m = _measure(html)
        print(f"  量尺：SVG @ ({m['x']},{m['y']}) {m['w']}×{m['h']} · 视口 {m['vpW']}×{m['vpH']}")
        # 与量尺同一个窗尺寸截图 → 布局一致，裁切区间才准
        _run([f"--window-size={W + SLACK_W},{H + SLACK_H}", f"--screenshot={raw}"], html)
        im = Image.open(raw).convert("RGB")
        box = (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
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
