# -*- coding: utf-8 -*-
"""渲染一张「独立高清全线地图」——把地图 SVG 按底图原生像素 1:1 出来。

为什么要单独出一张：
  攻略页里的地图受版式限制（左侧竖条）只能占 ~1163 设备像素宽，
  长图放大后必然糊。要想"放大看得清"，必须给一张**像素足够多**的独立地图。
  本脚本把同一份 SVG 按 3008 px 宽渲染 —— 正好等于自渲染底图的原生宽度。

⚠️ 底图必须 embed_max=0（不降采样）才是真正的 1:1。
   HTML 版为了控体积会把底图降到 1600 px 再内嵌，直接复用它渲染 3008 px，
   底图会被放大 1.88 倍、糊掉 —— 高清图的价值就没了（实测锐度差 52.8%）。
"""
import subprocess, sys, time
from pathlib import Path
from PIL import Image

ROOT = Path(r"C:/Users/S6576/WorkBuddy/2026-09-29-09-25-03")
sys.path.insert(0, str(ROOT / "geo"))
import map_svg                                                   # noqa: E402

OUTDIR = ROOT / "九华山南北穿越攻略"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

IW, IH = map_svg.IW, map_svg.IH
W = 3008                                   # = 底图原生宽（make_terrain 的 RASTER_W）
H = round(IH * W / IW)

html = OUTDIR / "_map_hi.html"
html.write_text(
    '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
    '*{margin:0;padding:0}body{background:#fff}'
    f'svg{{display:block;width:{W}px;height:{H}px}}'
    f'</style></head><body>{map_svg.build_svg(embed_image=True, embed_max=0)}</body></html>',
    encoding="utf-8")

raw = OUTDIR / "_map_hi.png"
raw.unlink(missing_ok=True)
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--no-sandbox", f"--window-size={W},{H}",
                f"--screenshot={raw}", html.as_uri()],
              check=True, capture_output=True, timeout=300)
time.sleep(0.5)

im = Image.open(raw).convert("RGB")
if im.size != (W, H):                      # 多截了就裁掉
    im = im.crop((0, 0, W, min(H, im.height)))
out = OUTDIR / "九华山南北穿越-全线地图.jpg"
im.save(out, quality=88, optimize=True, progressive=True, subsampling=0)
raw.unlink(missing_ok=True)
html.unlink(missing_ok=True)
print(f"{im.size} -> {out.name}  {out.stat().st_size/1048576:.2f} MB")
