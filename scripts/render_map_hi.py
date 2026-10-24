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

_SS = max(2, int(CFG.get("map_ss", 3) or 3))
W = MS.IW * _SS                     # 与 make_terrain 的超采样一致（逻辑宽 × SS = 栅格宽）
# ⚠ 独立高清图**要带名称带**（map_svg.TITLE_H = 132 逻辑 px）：这张图是单独发出去的，
#   没有标题就只剩一片地形与几根彩色线，收到的人不知道这是哪条线、几天、多长。
#   HTML 内嵌版不加（页面已有 h1），所以这里显式 with_title=True，画布高也必须用
#   out_size(True) —— 两处不一致会变成"图被上下压缩 / 留白"（2026-10-21 珠峰东坡实测：
#   旧版 this file 只算 IH，标题带从来没打开过，交付物一直是光板地图）。
_IH = MS.out_size(True)[1]
H = round(_IH * W / MS.IW)
SLACK_W, SLACK_H = 60, 260          # 给浏览器 chrome 留的余量（只用于开窗，不参与裁剪）

# ⚠ 超高画布（南北向线路，如库拉岗日 4512×23208）单窗截图会被无头浏览器的
#   软件光栅**静默截白**：2026-10-24 实测 4572×23468 窗口下内容只渲染到
#   ~13716 行（约 63 MP），之下整幅纯白、不报任何错 —— 全靠 _assert_full_frame 兜底。
#   → 页面面积超过安全阈值就**分带截图再拼接**（每带独立开窗、负 margin 平移裁剪）。
_MAX_SHOT_MP = 45 * 1_000_000       # 单次截图的页面面积安全上限（实测 31 MP 正常、107 MP 截白）


def _shot_bands():
    """返回 [(offset, height), ...]：超高画布的分带截图计划。"""
    if W * (H + SLACK_H) <= _MAX_SHOT_MP:
        return [(0, H)]
    band = (_MAX_SHOT_MP // W) - SLACK_H          # 每带窗口面积 ≤ 上限
    band = max(2000, band)
    bands, y = [], 0
    while y < H:
        bands.append((y, min(band, H - y)))
        y += band
    return bands


def _page_html(svg_path_svg, offset=0, band_h=None):
    band_h = band_h or H
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        '*{margin:0;padding:0}html,body{background:#fff}'
        f'.clip{{width:{W}px;height:{band_h}px;overflow:hidden}}'
        f'.clip>svg{{display:block;width:{W}px;height:{H}px;margin-top:{-offset}px}}'
        f'</style></head><body><div class="clip">{svg_path_svg}</div></body></html>')


def _assert_full_frame(a, tag):
    """断言：没有整行/整列近乎纯白（白带 = 内容被裁，页面白底露出来了）。"""
    white = (a > 246).all(2)
    worst_row = white.mean(1).max()
    worst_col = white.mean(0).max()
    if worst_row > 0.6 or worst_col > 0.6:
        raise RuntimeError(f"{tag}：有整行/整列近乎全白（行 {worst_row:.0%} / 列 {worst_col:.0%}）"
                           f"—— 内容被裁了，别交付")
    print(f"  满幅校验：最白行 {worst_row:.0%} · 最白列 {worst_col:.0%}（均应 < 60%）")


def _has_white_band(im):
    """单带快速白带检查：Chromium 软件光栅在高分幅下会**随机**丢瓦片
    （同参数重截即消失，2026-10-24 库拉岗日实测），所以这里不做断言、
    只返回布尔值供 main() 重试。"""
    a = np.asarray(im).astype(np.int16)
    white = (a > 246).all(2)
    return bool(white.mean(1).max() > 0.6 or white.mean(0).max() > 0.6)


def main():
    svg, _ = MS.build_svg(mobile=False, embed_image=True, embed_max=0, with_title=True)
    bands = _shot_bands()
    if len(bands) > 1:
        print(f"  超高画布 {W}×{H} → 分 {len(bands)} 带截图拼接（单窗光栅截白规避）")
    html = GC.ROOT / "_map_hi.html"
    raw = GC.ROOT / "_map_hi.png"
    try:
        t = time.time()
        parts = []
        for bi, (off, bh) in enumerate(bands):
            html.write_text(_page_html(svg, off, bh), encoding="utf-8")
            im = None
            for att in range(4):                    # 随机丢瓦片：命中白带就重截
                raw.unlink(missing_ok=True)
                # SVG 在 (0,0)、尺寸 W×H（body 白底、svg 无 margin）；开窗留余量，按 (0,0,W,bh) 裁。
                cand = GC.browser_shot(html, raw, W + SLACK_W, bh + SLACK_H, scale=1)
                cand = cand.crop((0, 0, W, bh))
                if cand.size != (W, bh):
                    raise RuntimeError(f"第 {bi+1} 带裁出来是 {cand.size}，应为 {(W, bh)}")
                im = cand
                if not _has_white_band(im):
                    break
                print(f"  第 {bi+1} 带检出白带（第 {att+1} 次截图）→ 重截")
            else:
                raise RuntimeError(f"第 {bi+1} 带连续 4 次截出白带 —— 请缩小地图窗口或 map_ss")
            parts.append(im)
        im = parts[0] if len(parts) == 1 else _vstack(parts)
        _assert_full_frame(np.asarray(im).astype(np.int16), "高清地图")
        im.save(OUT_JPG, quality=88, optimize=True, progressive=True, subsampling=0)
        print(f"  {im.size} -> {OUT_JPG.name}  {OUT_JPG.stat().st_size/1048576:.2f} MB  "
              f"{time.time()-t:.1f}s")
    finally:
        raw.unlink(missing_ok=True)
        html.unlink(missing_ok=True)


def _vstack(parts):
    out = Image.new("RGB", (W, H), "#ffffff")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.size[1]
    return out


if __name__ == "__main__":
    main()
