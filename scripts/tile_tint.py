# -*- coding: utf-8 -*-
"""B 路线：把在线 XYZ 瓦片改成"地形图管线"底图 —— 抹掉瓦片自带等高线，用本地 DEM 重画。

什么时候需要它
--------------
用户点名要用某个在线瓦片服务（见 `references/tile-basemap.md`）时，会撞上两件必踩的事：
  · 瓦片自带的等高线在攻略图幅下**要么太淡（高海拔浅色区几乎看不见）、要么太密**
    （陡坡区线密度到 13–24%，视觉上抢戏），而且想"只留计曲线"在像素层面筛不出来；
  · 瓦片是"一色米底"，整张图看不出高差，用户会说"地图基本一个颜色，没有地形参考"。

正确解法是地形图的标准做法：**让瓦片只贡献"线画"（步道 / 公路 / 文字 / 水体），
面积底色全部换成 DEM 分层设色 + 晕渲，等高线用本地 DEM 自己画。**
本脚本就是这一步的可执行实现（此前只散落在 `references/tile-basemap.md` §7/§11 的叙述里）。

它**不下载瓦片** —— 拼接瓦片是 `build_base_map.py`（或 `tiles.py`）的事。
本脚本吃已经拼好的整块底图，输出与 A 路线**完全同 schema** 的 `out/base_map.jpg`
+ `out/base_meta.json`，所以下游（map_svg / render_map_hi）不用改一行。

用法：
    python tile_tint.py --tile out/base_tile.jpg      # 主用法
    python tile_tint.py --tile out/base_tile.jpg --no-erase   # 保留瓦片自带等高线
    python tile_tint.py --selftest                    # 合成图自检（不联网、不依赖项目数据）
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import guide_common as GC                                            # noqa: E402
import make_terrain as MT                                            # noqa: E402

OUT = HERE / "out"

# ---- 瓦片自带等高线的色彩掩码 ----
# 亮橙棕（等高线 + 部分标注文字）。**g>90 是关键**：深棕步道的 g≈70，
# 少了这一条会把步道一起吃掉 —— 这是此管线最容易翻车的地方。
ERASE_R_MIN, ERASE_G_MIN = 140, 90
ERASE_RB_MIN, ERASE_RG_MIN, ERASE_B_MAX = 50, 30, 210
ERASE_DILATE = 1        # 掩码膨胀（连抗锯齿边一起填掉）
ERASE_ROUNDS = 12       # 迭代邻域均值填充轮数（~10 轮收敛）

# ---- 线画掩码 ----
LINE_BLUR = 7           # 局部背景的估计窗口；比局部背景暗 = 线画
LINE_BIAS = 5.0         # 亮度差先减掉 5：抗锯齿的淡边不算线画
LINE_GAIN = 20.0        # 差 20 灰阶即视为满强度线画
LINE_THIN = 1.15        # 细线增厚系数：瓦片线画普遍偏细，糊一层才压得住 DEM 设色
HUE_MARGIN = 12         # 偏绿 / 偏蓝多少算"色相线画"（林地、水体、冰川）

JPG = MT.JPG


# ------------------------------------------------------------------ 通用
def _blur_lum(lum_img, r=LINE_BLUR):
    """PIL 的 BoxBlur 对 "L" 模式可用；F 模式不支持，所以先转 L。"""
    return np.asarray(lum_img.filter(ImageFilter.BoxBlur(r)), dtype=np.float32)


def luminance(rgb):
    a = rgb.astype(np.float32)
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


# ------------------------------------------------------------------ ① 抹除瓦片自带等高线
def contour_mask(rgb, dilate=ERASE_DILATE):
    """瓦片里"亮橙棕的等高线与注记"掩码。返回 bool 数组。"""
    a = np.asarray(rgb).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = ((r > ERASE_R_MIN) & (g > ERASE_G_MIN)
         & (r - b > ERASE_RB_MIN) & (r - g > ERASE_RG_MIN) & (b < ERASE_B_MAX))
    for _ in range(int(dilate)):        # 十字膨胀，连抗锯齿的淡边一起吃掉
        o = m.copy()
        o[1:, :] |= m[:-1, :]
        o[:-1, :] |= m[1:, :]
        o[:, 1:] |= m[:, :-1]
        o[:, :-1] |= m[:, 1:]
        m = o
    return m


def erase_contours(rgb, dilate=ERASE_DILATE, rounds=ERASE_ROUNDS, verbose=True):
    """抹掉瓦片自带等高线：色彩掩码 → 膨胀 → 迭代邻域均值填充。

    ⚠ 取邻域**必须用 edge-padding**（`np.pad(mode="edge")`）。
      用 `np.roll` 回绕会把对边像素填进图像边缘，接缝处出现一串虚线点。
    """
    m = contour_mask(rgb, dilate)
    frac = m.mean()
    out = np.asarray(rgb).astype(np.float32).copy()
    live = m.copy()
    h, w = m.shape
    for i in range(int(rounds)):
        if not live.any():
            break
        pa = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge")
        pm = np.pad(live, 1, mode="edge")
        acc = np.zeros_like(out)
        cnt = np.zeros((h, w), np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):   # 4 邻域
            nb = pa[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
            ok = (~pm[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]) & live
            acc += nb * ok[..., None]
            cnt += ok
        upd = live & (cnt > 0)
        out[upd] = acc[upd] / cnt[upd][:, None]
        live &= ~upd
    if verbose:
        print(f"   抹除瓦片等高线：命中 {frac * 100:.1f}% 像素，"
              f"{int(rounds)} 轮填充后残余 {int(live.sum())} px")
    return out.astype(np.uint8)


# ------------------------------------------------------------------ ② 线画掩码
def lineart_keep(rgb, blur=LINE_BLUR, bias=LINE_BIAS, gain=LINE_GAIN,
                 thin=LINE_THIN, hue_margin=HUE_MARGIN, verbose=True):
    """从瓦片里抠出"线画 + 本色块"的保留系数（0–1）。

    线画 = 比**局部背景**更暗的像素：局部背景用 BoxBlur 估计，
    亮度差 -bias 后除以 gain 再 clip。步道、公路、文字、溪流一次全抓；
    另外按色相保留偏绿（林地）/偏蓝（水体、冰川）的色块，否则底色会被设色冲掉。
    """
    a = np.asarray(rgb).astype(np.float32)
    lum = luminance(a)
    bg = _blur_lum(Image.fromarray(lum.astype(np.uint8), "L"), blur)
    line = np.clip((bg - lum - bias) / gain, 0, 1)

    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    hue = (((g - r > hue_margin) & (g - b > hue_margin))
           | ((b - r > hue_margin) & (b - g > hue_margin))).astype(np.float32)
    keep = np.maximum(line, hue)

    if thin and thin > 1.0:             # 细线增厚：瓦片线画普遍 1 px，直接合成会被设色盖住
        pil = Image.fromarray((keep * 255).astype(np.uint8), "L")
        big = np.asarray(pil.resize((pil.width * 2, pil.height * 2), Image.BILINEAR),
                         dtype=np.float32) / 255.0
        keep = np.maximum(keep, big[::2, ::2])
        # 上采样后抽回原尺寸 = 变宽一点点的软边，比直接 max-pool 更自然
        keep = np.clip(keep, 0, 1)

    if verbose:
        print(f"   线画掩码：覆盖 {keep.mean() * 100:.1f}% 面积"
              f"（其中色相块 {(hue > 0).mean() * 100:.1f}%）")
    return keep


# ------------------------------------------------------------------ ③ 合成
def composite(tile_rgb, keep, tint_rgb):
    """线画压在上层，DEM 设色垫底。keep=1 处是瓦片原样，keep=0 处是纯设色。"""
    k = keep[..., None]
    return np.clip(np.asarray(tile_rgb, np.float32) * k
                   + np.asarray(tint_rgb, np.float32) * (1 - k), 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ 主流程
def read_tile(path, size):
    """读拼好的瓦片底图并缩放到 DEM 栅格尺寸。

    两者必须覆盖**同一个 bbox**（build_base_map 与 make_terrain 共用 resolve_bbox，
    所以默认就一致）。长宽比差得多说明它其实是别的窗口 —— 那不该硬缩，
    缩了会让瓦片线画与 DEM 地形整体错位，且不报错。
    """
    im = Image.open(path).convert("RGB")
    tw, th = im.size
    dw, dh = size
    if abs((tw / th) / (dw / dh) - 1) > 0.02:
        raise SystemExit(
            f"瓦片底图长宽比 {tw / th:.3f} 与 DEM 窗口 {dw / dh:.3f} 差得太多。\n"
            f"  → 两者不是同一个 bbox。先确认 build_base_map.py 与 make_terrain.py "
            f"都走 guide_common.resolve_bbox()（同一个窗口）。")
    return np.asarray(im.resize((dw, dh), Image.LANCZOS), dtype=np.uint8)


def build(tile_path=None, erase=True, verbose=True):
    LON0, LAT0, LON1, LAT1, src = GC.resolve_bbox()
    GC.assert_bbox_covers((LON0, LAT0, LON1, LAT1), "tile_tint")
    print(f"[1/6] 地图窗口（{src}）: {LON0}, {LAT0} → {LON1}, {LAT1}", flush=True)

    dem, meta = MT.load_dem(MT.DEM_Z, LON0, LAT0, LON1, LAT1)
    dem = MT.fill_voids(dem)
    lat_c = (LAT0 + LAT1) / 2
    lo, hi = float(np.nanmin(dem)), float(np.nanmax(dem))

    dw, dh = dem.shape[1], dem.shape[0]
    scale_logical = MT.LOGICAL_W / dw
    scale_raster = MT.RASTER_W / dw
    LOGICAL_H = round(dh * scale_logical)
    RASTER_H = round(dh * scale_raster)

    print("[2/6] 晕渲 + 分层设色（DEM 侧，与 A 路线同一套 ramp）…")
    hs = MT.hillshade(dem, meta["z"], lat_c)
    rgb = MT.hypso(dem)
    tint = rgb * (MT.AMBIENT + MT.DIRECTIONAL * hs)[..., None]
    over = tint > MT.HL_CLIP
    tint[over] = MT.HL_CLIP + (tint[over] - MT.HL_CLIP) * MT.HL_KEEP
    tint = np.clip(tint, 0, 255).astype(np.uint8)
    tint = np.asarray(Image.fromarray(tint, "RGB").resize((MT.RASTER_W, RASTER_H),
                                                          Image.BICUBIC))

    if tile_path:
        print(f"[3/6] 读取瓦片底图 {Path(tile_path).name} …")
        tile = read_tile(tile_path, (MT.RASTER_W, RASTER_H))
        if erase:
            tile = erase_contours(tile, verbose=verbose)
        else:
            print("   按要求保留瓦片自带等高线")
        keep = lineart_keep(tile, verbose=verbose)
        print("[4/6] 合成：瓦片线画 + DEM 设色 …")
        img = composite(tile, keep, tint)
    else:
        print("[3/6] 未给 --tile：退化为纯 DEM 底图（等价于 make_terrain 的 A 路线）")
        img = tint

    print("[5/6] 本地 DEM 重绘等高线 + 高程标注（自绘的线才与地形严格同位）…")
    dem_out = np.asarray(Image.fromarray(dem, "F").resize((MT.RASTER_W, RASTER_H),
                                                          Image.LANCZOS), dtype=np.float32)
    arr = img.copy()
    c_minor, c_major, _ = MT.pick_contour_interval(dem_out)
    MT.paint_contours(arr, dem_out, *MT.CONTOUR_DILATE, minor=c_minor, major=c_major)
    pil = Image.fromarray(arr, "RGB")
    scale_raster_y = RASTER_H / dh
    pil, seed_cells = MT.draw_contour_labels(
        pil, dem, (scale_raster, scale_raster_y), lo, hi,
        avoid=MT.avoid_points(meta, scale_raster, MT.SS), every=c_major)

    m = {"size": [MT.LOGICAL_W, LOGICAL_H], "scale": scale_logical,
         "raster": [MT.RASTER_W, RASTER_H], "ss": MT.SS,
         "box": meta["box"], "bbox": [LON0, LAT0, LON1, LAT1], "z": meta["z"],
         "tx0": meta["tx0"], "ty0": meta["ty0"],
         "contour": {"minor": c_minor, "major": c_major},
         "source": ("在线瓦片（仅保留线画）+ 本地 DEM 分层设色与等高线"
                    if tile_path else f"本地自渲染：地形 DEM z{MT.DEM_Z} + OSM 矢量"),
         "attribution": "Elevation: SRTM/Copernicus DEM (AWS Open Data) · "
                        "Map data: © OpenStreetMap contributors (ODbL)"}

    try:
        from draw_osm import draw_overlay
        import route_def as _RD
        skip = {d["name"] for d in GC.pois_of(_RD)}
        pil = draw_overlay(pil, m, OUT / "osm.json", skip_names=skip,
                           px_scale=MT.SS, proj_scale=scale_raster, seed_cells=seed_cells)
    except FileNotFoundError:
        pass

    pil.save(OUT / "base_map.jpg", **JPG)
    (OUT / "base_meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    print(f"[6/6] 完成 栅格 {pil.size} / 逻辑 {MT.LOGICAL_W}×{LOGICAL_H}  "
          f"高程 {lo:.0f}–{hi:.0f} m  jpg "
          f"{(OUT / 'base_map.jpg').stat().st_size / 1048576:.2f} MB")
    return m


# ------------------------------------------------------------------ 自检
def _selftest():
    """合成一张"假瓦片"跑全链，断言三件事：等高线被抹掉、线画被保住、底色换成设色。

    为什么要自带自检：本脚本的输入是在线瓦片，现场很可能拉不到（服务挂、限流）；
    但"抹得干不干净、步骤有没有走反"是**纯算法问题**，用合成图就能验。
    """
    print("=== tile_tint 自检（合成图，不联网）===")
    h, w = 240, 360
    beige = np.array([242, 236, 222], np.uint8)
    tile = np.tile(beige, (h, w, 1)).astype(np.int16)

    # ① 瓦片自带等高线：亮橙棕，符合 ERASE 掩码
    orange = np.array([205, 140, 90])
    for x in range(20, w, 40):
        tile[:, x - 1:x + 2] = orange
    n_orange = int((np.abs(tile - orange).sum(2) == 0).sum())

    # ② 步道：深棕，g≈70 —— 必须**不被**抹除（这是 g>90 那条判据的意义）
    trail = np.array([120, 70, 45])
    tile[120:124, :] = trail

    # ③ 文字：灰黑，也属于线画
    tile[40:48, 60:140] = np.array([70, 68, 66])

    rgb = tile.astype(np.uint8)
    n_trail0 = int((np.abs(rgb.astype(int) - trail).sum(2) == 0).sum())

    erased = erase_contours(rgb, verbose=False)
    a = erased.astype(int)
    n_orange_left = int((np.abs(a - orange).sum(2) == 0).sum())
    n_trail_left = int((np.abs(a - trail).sum(2) == 0).sum())

    keep = lineart_keep(rgb, verbose=False)
    # 线画处的 keep 应接近 1，纯底色处应接近 0
    k_line = float(keep[121, 100])
    k_bg = float(keep[200, 10])

    tint = np.zeros((h, w, 3), np.uint8)
    tint[..., 1] = 200                       # 假 DEM 设色：纯绿
    out = composite(erased, keep, tint)
    o_line = out[121, 100]
    o_bg = out[200, 10]

    print(f"  等高线像素 {n_orange} → 残留 {n_orange_left}"
          f"（{n_orange_left / max(n_orange, 1) * 100:.1f}%）")
    print(f"  步道像素 {n_trail0} → 保留 {n_trail_left}"
          f"（{n_trail_left / max(n_trail0, 1) * 100:.1f}%）")
    print(f"  keep：线画 {k_line:.2f} / 底色 {k_bg:.2f}")
    print(f"  合成：线画处 RGB {tuple(o_line)} / 底色处 RGB {tuple(o_bg)}")

    ok = True
    def chk(name, cond):
        nonlocal ok
        print(f"  [{'✓' if cond else '✗'}] {name}")
        ok &= bool(cond)

    chk("抹除率 > 95%", n_orange_left / max(n_orange, 1) < 0.05)
    chk("步道保留率 > 95%（g>90 判据生效）", n_trail_left / max(n_trail0, 1) > 0.95)
    chk("线画 keep > 0.8", k_line > 0.8)
    chk("底色 keep < 0.05", k_bg < 0.05)
    chk("底色被换成设色（偏绿）", int(o_bg[1]) > 150 and int(o_bg[0]) < 80)
    chk("线画处仍是深棕（没被设色冲掉）", int(o_line[0]) < 180 and int(o_line[1]) < 140)
    chk("抹除后底色变回米色（填充有生效）", int(np.abs(a[80, 10] - beige).sum()) < 30)
    print("=== 自检" + ("通过 ✓" if ok else "失败 ✗") + " ===")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B 路线：瓦片线画分离 + 本地 DEM 自绘地形底图")
    ap.add_argument("--tile", help="已拼好的瓦片底图（如 out/base_tile.jpg）")
    ap.add_argument("--no-erase", action="store_true",
                    help="保留瓦片自带等高线（默认抹除后用本地 DEM 重画）")
    ap.add_argument("--selftest", action="store_true", help="合成图自检，不联网、不读项目数据")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    if not a.tile:
        print("提示：没给 --tile，将退化为纯 DEM 底图（与 A 路线等价）。\n"
              "      要用在线瓦片请先跑 build_base_map.py 拼图，再 --tile out/base_tile.jpg")
    build(a.tile, erase=not a.no_erase)


if __name__ == "__main__":
    main()
