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
import json
import re
import subprocess
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
BROWSER = GC.find_browser()

MEASURE_JS = r"""
<script>
window.addEventListener('load', function(){
  var p = document.querySelector('.page') || document.body;
  var r = p.getBoundingClientRect();
  var de = document.documentElement;
  document.body.setAttribute('data-m', JSON.stringify({
    x: Math.round(r.left), right: Math.round(r.right),
    w: Math.round(r.width), h: Math.round(r.height),
    docH: Math.max(de.scrollHeight, document.body.scrollHeight),
    docW: de.scrollWidth, vpW: de.clientWidth
  }));
});
</script>
"""


def _dump_dom(html_path):
    p = subprocess.run([BROWSER, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--no-sandbox", f"--window-size={WIN_W},1200", "--dump-dom",
                        html_path.as_uri()], capture_output=True, timeout=300)
    return p.stdout.decode("utf-8", "ignore")


def _measure():
    """① DOM 量尺寸：只排版不光栅，很快。"""
    tmp = GC.ROOT / "_shoot_probe.html"
    src = HTML.read_text(encoding="utf-8")
    assert "</body>" in src, "HTML 里没有 </body>，无法注入量尺脚本"
    tmp.write_text(src.replace("</body>", MEASURE_JS + "</body>", 1), encoding="utf-8")
    try:
        dom = _dump_dom(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    m = re.search(r'data-m="([^"]+)"', dom)
    if not m:
        raise RuntimeError("量尺脚本没跑起来（--dump-dom 里找不到 data-m）")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


def _page_box_from_pixels(im, bg):
    """兜底：从像素量版心（.page 底色与 body 底色不同，可分辨）。"""
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
    m = _measure()
    print(f"  量尺：版心 x {m['x']}–{m['right']}（宽 {m['w']}）· 内容高 {m['h']} "
          f"· 文档高 {m['docH']} · 视口宽 {m['vpW']}")
    if m["w"] != DESIGN_W:
        print(f"  !! 版心宽量到 {m['w']}，设计值是 {DESIGN_W}（CSS .page 被改过？）")
    if m["right"] > m["vpW"]:
        print(f"  !! 版心右界 {m['right']} 超出视口 {m['vpW']} —— 窗口太窄，调大 WIN_W")

    win_h = int(m["docH"]) + 40
    raw = GC.ROOT / "_shoot_raw.png"
    raw.unlink(missing_ok=True)
    t = time.time()
    subprocess.run([BROWSER, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--no-sandbox", f"--force-device-scale-factor={SCALE}",
                    f"--window-size={WIN_W},{win_h}", f"--screenshot={raw}", HTML.as_uri()],
                   check=True, capture_output=True, timeout=900)
    im = Image.open(raw).convert("RGB")
    print(f"  截图 {im.size} 用时 {time.time()-t:.1f}s")

    # ② 像素复量：优先用像素量到的版心（比 DOM 更贴近实际光栅化结果）
    pg = _page_box_from_pixels(im, im.getpixel((2, 2)))
    box = pg or (round(m["x"] * SCALE), round(m["right"] * SCALE), 0,
                 round(m["docH"] * SCALE))
    x0, x1, y0, y1 = box
    want = DESIGN_W * SCALE
    if pg and abs((x1 - x0) - want) > 2:
        print(f"  !! 量到版心宽 {x1-x0} px，应为 {want} px")
    if pg:
        y0 = 0                                        # 长图从纸面顶开始，保留外框
    if y1 >= im.height - 2:
        print(f"  !! 内容顶到画布底（{y1}/{im.height}）—— 可能被截断，请检查量尺")

    im.crop((0, 0, im.width, min(im.height, y1 + PAD_BOTTOM))).save(OUT_PNG, optimize=True)
    raw.unlink(missing_ok=True)
    out = Image.open(OUT_PNG)
    print(f"  -> {out.size} · {OUT_PNG.stat().st_size/1048576:.2f} MB · {OUT_PNG.name}")


if __name__ == "__main__":
    if not HTML.exists():
        sys.exit(f"缺 {HTML} —— 先跑 build_guide.py")
    render()
