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
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

import guide_common as GC
import map_svg as MS
import route_def as RD

CFG = getattr(RD, "CFG", {})


def _rm_quiet(p):
    """临时文件清理是尽力而为：环境层的 SAFE_DELETE 钩子按「单轮累计删除量」拦截，
    超阈值直接把进程杀掉（异常抛不出去）——批量出图（≥25 条线路）必然踩到，
    表现是"图都渲完了、最后清临时文件时进程退出码 1、成品没落盘"。
    → SHOOT_KEEP_RAW=1 时一律不删（与 shoot_guide 同一约定，批量跑完再统一清）。"""
    if os.environ.get("SHOOT_KEEP_RAW"):
        return
    Path(p).unlink(missing_ok=True)
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
#   → 页面面积超过安全阈值就**分带截图再拼接**（每带裁 viewBox，见 _page_html）。
_MAX_SHOT_MP = 45 * 1_000_000       # 单次截图的页面面积安全上限（实测 31 MP 正常、107 MP 截白）


def _shot_bands():
    """返回 [(剪裁起点, 本带高), ...]，单位都是**逻辑 px**（viewBox 坐标系）。

    ⚠ 必须按逻辑 px 规划、物理高按累计 round 推：viewBox 的起点若不是整数，
      接缝处会因半像素重采样出现一条发虚的细线；直接 round 每带高度又会与总高
      H 差 1–2 px（最后一带被压扁或留白）。
    """
    if W * (H + SLACK_H) <= _MAX_SHOT_MP:
        return [(0, _IH)]
    band_px = (_MAX_SHOT_MP // W) - SLACK_H          # 每带窗口面积 ≤ 上限
    band_logical = max(600, int(band_px // _SS))
    bands, y = [], 0
    while y < _IH:
        bands.append((y, min(band_logical, _IH - y)))
        y += band_logical
    return bands


_VIEWBOX_RE = re.compile(r'viewBox="0 0 ([\d.]+) ([\d.]+)"')


def _page_html(svg_str, oy=0, bh=None):
    """把「一条 SVG 到底」的地图裁成**只含 oy…oy+bh 这一段**的页面，返回 (html, 物理高)。

    ⚠⚠ 不要再用「负 margin-top 平移整幅 SVG」的老做法。Chromium 的软件光栅在
      SVG 高度超过 ~16384 px 后，**超限部分不按偏移重画**：画面静默复用前面
      已经栅格化好的内容。2026-10-26 库拉岗日（4512×23208，三带）实测：
      第 3 带（起点 19426）画出来的是 y≈9700 那一段 —— 接缝处一条横贯全幅的
      「绿坡 / 雪山」断层，既不报错，`_has_white_band` 也全绿（没有白带，
      只是内容错了）。块匹配可量化：该带内容与 base_map 的 y≈9248 几乎逐像素相同。
      → 每带只把根节点的 viewBox 裁到本段，SVG 自身物理高度 ≪ 上限，内容不可能错位。
    """
    bh = bh or _IH
    h_px = round((oy + bh) * H / _IH) - round(oy * H / _IH)
    svg = _VIEWBOX_RE.sub(f'viewBox="0 {oy} {MS.IW} {bh}"', svg_str, count=1)
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
            '*{margin:0;padding:0}html,body{background:#fff}'
            f'.clip{{width:{W}px;height:{h_px}px;overflow:hidden}}'
            f'.clip>svg{{display:block;width:{W}px;height:{h_px}px}}'
            f'</style></head><body><div class="clip">{svg}</div></body></html>'), h_px


# 白底外圈：SVG 被浏览器缩放到几千 px 宽时，最外 1–2 px 会因插值露出页面白底
# （2026-10-24 大横断实测：仅第 0 列与最后一列 >60% 白，其余全有内容 —— 不是裁切）。
# 判白带时忽略外圈；真被裁时白带宽几十 px，照样抓得到。
_EDGE_IGNORE = 3


def _white_ratio(a):
    """去掉外圈后的最白行 / 最白列比例。"""
    if a.shape[0] <= 2 * _EDGE_IGNORE or a.shape[1] <= 2 * _EDGE_IGNORE:
        white = (a > 246).all(2)
    else:
        white = (a[_EDGE_IGNORE:-_EDGE_IGNORE, _EDGE_IGNORE:-_EDGE_IGNORE] > 246).all(2)
    return white.mean(1).max(), white.mean(0).max()


def _assert_full_frame(a, tag):
    """断言：没有整行/整列近乎纯白（白带 = 内容被裁，页面白底露出来了）。"""
    worst_row, worst_col = _white_ratio(a)
    if worst_row > 0.6 or worst_col > 0.6:
        raise RuntimeError(f"{tag}：有整行/整列近乎全白（行 {worst_row:.0%} / 列 {worst_col:.0%}）"
                           f"—— 内容被裁了，别交付")
    print(f"  满幅校验：最白行 {worst_row:.0%} · 最白列 {worst_col:.0%}（均应 < 60%，"
          f"外圈 {_EDGE_IGNORE}px 不计）")


def _has_white_band(im):
    """单带快速白带检查：Chromium 软件光栅在高分幅下会**随机**丢瓦片
    （同参数重截即消失，2026-10-24 库拉岗日实测），所以这里不做断言、
    只返回布尔值供 main() 重试。"""
    worst_row, worst_col = _white_ratio(np.asarray(im).astype(np.int16))
    return bool(worst_row > 0.6 or worst_col > 0.6)


def _assert_seams(im, parts):
    """拼接缝自检：接缝那一行的整行色差，不该显著高于全图中位行差。

    为什么必须有（2026-10-26 库拉岗日实测）：带内容错位时**一条白带都没有** ——
    `_assert_full_frame` 全绿、`_has_white_band` 也全绿，只有接缝处横贯全幅的地形断层。
    判据：d[seam] > max(6×中位行差, 18) 即失败。真实地形不可能让 4512 px 宽的
    一整行同时剧变（那要求全幅等高线、植被、晕渲同时换向）；库拉岗日事故是 41.2 vs 中位 3.5（≈12×）。
    """
    if len(parts) < 2:
        return
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    d = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    med = float(np.median(d))
    thr = max(6 * med, 18.0)
    ys, y = [], 0
    for p in parts[:-1]:
        y += p.size[1]
        ys.append(y)
    bad = [(y, float(d[y])) for y in ys if y < len(d) and d[y] > thr]
    if bad:
        raise RuntimeError(
            "拼接缝处整行剧变，疑似带内容错位（不是白带，白带检测抓不到）："
            + "、".join(f"y={y} 行差 {v:.1f}" for y, v in bad)
            + f"（全图中位 {med:.1f}，阈值 {thr:.1f}）—— 检查 _page_html 是否按 viewBox 裁带")
    print("  拼接缝自检："
          + "、".join(f"y={y} 行差 {float(d[y]):.1f}" for y in ys if y < len(d))
          + f"（全图中位 {med:.1f}）✓")


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
        for bi, (oy, bh) in enumerate(bands):
            page, h_px = _page_html(svg, oy, bh)
            html.write_text(page, encoding="utf-8")
            im = None
            for att in range(4):                    # 随机丢瓦片：命中白带就重截
                _rm_quiet(raw)                      # keep-raw 模式下由 browser_shot 按 mtime 判新
                # 本带 SVG 的 viewBox 已裁到 oy…oy+bh，物理尺寸 W×h_px、位于页面 (0,0)
                cand = GC.browser_shot(html, raw, W + SLACK_W, h_px + SLACK_H, scale=1)
                cand = cand.crop((0, 0, W, h_px))
                if cand.size != (W, h_px):
                    raise RuntimeError(f"第 {bi+1} 带裁出来是 {cand.size}，应为 {(W, h_px)}")
                im = cand
                if not _has_white_band(im):
                    break
                print(f"  第 {bi+1} 带检出白带（第 {att+1} 次截图）→ 重截")
            else:
                raise RuntimeError(f"第 {bi+1} 带连续 4 次截出白带 —— 请缩小地图窗口或 map_ss")
            parts.append(im)
        im = parts[0] if len(parts) == 1 else _vstack(parts)
        if im.size != (W, H):
            raise RuntimeError(f"拼接结果 {im.size} ≠ 期望 {(W, H)}（分带高度累计有误）")
        _assert_full_frame(np.asarray(im).astype(np.int16), "高清地图")
        _assert_seams(im, parts)
        # 高清图是**重渲 SVG**、不复用 base_map.jpg —— 水印必须在这里再烧一次，
        # 否则高清图会裸奔（只加 base_map 的话，长图有、高清图没有）。
        im = GC.burn_watermark(im)
        im.save(OUT_JPG, quality=int(CFG.get("jpg_quality", 88) or 88),
                optimize=True, progressive=True, subsampling=0,
                **GC.jpg_meta(f'{CFG.get("title", STEM)} · 全线地图 · 非导航用图'))
        print(f"  {im.size} -> {OUT_JPG.name}  {OUT_JPG.stat().st_size/1048576:.2f} MB  "
              f"{time.time()-t:.1f}s")
    finally:
        _rm_quiet(raw)
        _rm_quiet(html)


def _vstack(parts):
    out = Image.new("RGB", (W, H), "#ffffff")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.size[1]
    return out


if __name__ == "__main__":
    main()
