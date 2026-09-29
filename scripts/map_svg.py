# -*- coding: utf-8 -*-
"""生成「九华山南北穿越」地图 SVG（真底图 + 实测轨迹 + 标注）"""
import base64, json, math
from pathlib import Path
from build_map import projector, OUT, LAT0, LAT1, Z
from route_def import build_route, POIS, SPLIT_KM, fmt_ele

HERE = Path(__file__).resolve().parent
meta = json.loads((OUT / "base_meta.json").read_text(encoding="utf-8"))
IW, IH = meta["size"]
PROJ = projector(IW, IH, meta["scale"], meta["box"], {"tx0": meta["tx0"], "ty0": meta["ty0"]})

C1 = "#E4572E"
C2 = "#1E6FD9"
WARN = "#F26B21"
_M_PER_PX = 156543.03392 * math.cos(math.radians((LAT0 + LAT1) / 2)) / (2 ** Z) / meta["scale"]


def P(lon, lat):
    x, y = PROJ(lon, lat)
    return round(x, 1), round(y, 1)


def d_of(pts):
    return "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)


# 点位符号的统一下调系数：线路减细后，原来的大圆点会显得"头重脚轻"，
# 符号与线宽需要按视觉层级重新配比（线细了，点也得跟着收）。
PS = 0.85


def mk_pin(x, y, kind):
    if kind == "start":
        r = 19 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#D93B2B" stroke="#fff" stroke-width="{5*PS:.1f}"/>'
                f'<text x="{x}" y="{y+8*PS:.1f}" text-anchor="middle" font-size="{21*PS:.1f}" font-weight="800" fill="#fff">S</text>')
    if kind == "end":
        r = 19 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#0F6E5C" stroke="#fff" stroke-width="{5*PS:.1f}"/>'
                f'<text x="{x}" y="{y+8*PS:.1f}" text-anchor="middle" font-size="{21*PS:.1f}" font-weight="800" fill="#fff">E</text>')
    if kind in ("peak", "peak_hi"):
        hi = kind == "peak_hi"
        col = "#B23A1C" if hi else "#4A5261"
        r = (19 if hi else 15) * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r+5*PS:.1f}" fill="#fff" opacity="0.9"/>'
                f'<path d="M {x-r:.1f},{y+r*0.62:.1f} L {x},{y-r*0.78:.1f} L {x+r:.1f},{y+r*0.62:.1f} Z" fill="{col}"/>')
    if kind == "temple":
        return (f'<circle cx="{x}" cy="{y}" r="{20*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{15.5*PS:.1f}" fill="#3A4250"/>'
                f'<path d="M {x-10*PS:.1f},{y-1.5*PS:.1f} L {x},{y-11*PS:.1f} L {x+10*PS:.1f},{y-1.5*PS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-6.5*PS:.1f}" y="{y+1*PS:.1f}" width="{13*PS:.1f}" height="{8*PS:.1f}" fill="#fff"/>')
    if kind == "camp":
        return (f'<circle cx="{x}" cy="{y}" r="{20*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{15.5*PS:.1f}" fill="{C1}"/>'
                f'<path d="M {x-10*PS:.1f},{y+8*PS:.1f} L {x},{y-9*PS:.1f} L {x+10*PS:.1f},{y+8*PS:.1f} Z" fill="#fff"/>')
    if kind == "water":
        return (f'<circle cx="{x}" cy="{y}" r="{20*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{15.5*PS:.1f}" fill="#0F8A9B"/>'
                f'<path d="M {x},{y-9*PS:.1f} C {x+9*PS:.1f},{y+1*PS:.1f} {x+7*PS:.1f},{y+9*PS:.1f} {x},{y+9*PS:.1f} '
                f'C {x-7*PS:.1f},{y+9*PS:.1f} {x-9*PS:.1f},{y+1*PS:.1f} {x},{y-9*PS:.1f} Z" fill="#fff"/>')
    if kind == "warn":
        return (f'<circle cx="{x}" cy="{y}" r="{20*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{15.5*PS:.1f}" fill="{WARN}"/>'
                f'<text x="{x}" y="{y+7*PS:.1f}" text-anchor="middle" font-size="{21*PS:.1f}" font-weight="900" fill="#fff">!</text>')
    return ""


def build_overlay():
    r = build_route()
    pts = r["pts"]
    XY = [P(*p) for p in pts]
    kms = r["km"]
    split = r["split"]

    # 线宽是"地图 vs 底图"的平衡点：太粗会压掉地形与步道，太细在缩略尺寸下看不见。
    # 底图改为 3008 栅格 / 1504 逻辑之后，同样的 7.5 在 1:1 高清图里显得很笨重，
    # 收到 5.0（缩到攻略页 ~581 CSS px 时约 2 CSS px）——刚好是"一眼找到、又不挡地形"。
    ROUTE_W, HALO_W = 5.0, 8.0
    o = [f'<path d="{d_of(XY)}" fill="none" stroke="#FFFFFF" stroke-width="{HALO_W}" '
         f'stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/>']
    for seg, col in ((XY[:split + 1], C1), (XY[split:], C2)):
        if len(seg) < 2:
            continue
        o.append(f'<path d="{d_of(seg)}" fill="none" stroke="{col}" stroke-width="{ROUTE_W}" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')

    # 沿线每 5 km 打一个小里程点
    for target in range(5, int(r["total_km"]) + 1, 5):
        i = min(range(len(kms)), key=lambda j: abs(kms[j] - target))
        x, y = XY[i]
        o.append(f'<circle cx="{x}" cy="{y}" r="7.4" fill="#FFFFFF" stroke="#3A4250" stroke-width="2"/>'
                 f'<text x="{x}" y="{y+4.2}" text-anchor="middle" font-size="10" font-weight="800" '
                 f'fill="#3A4250">{target}</text>')

    # D1 / D2 分界短线
    sx, sy = XY[split]
    o.append(f'<line x1="{sx-56}" y1="{sy}" x2="{sx+56}" y2="{sy}" stroke="#FFFFFF" '
             f'stroke-width="7" stroke-linecap="round"/>'
             f'<line x1="{sx-56}" y1="{sy}" x2="{sx+56}" y2="{sy}" stroke="#6C7480" '
             f'stroke-width="2.4" stroke-dasharray="11 8" stroke-linecap="round"/>')

    for name, lon, lat, ele, k, dx, dy, anc in POIS:
        x, y = P(lon, lat)
        o.append(mk_pin(x, y, k))
        fs = (30 if k in ("start", "end") else 28) * PS
        col = "#B23A1C" if k == "peak_hi" else ("#0F6E5C" if k in ("start", "end") else "#1F2430")
        txt = f"{name}<tspan font-size='{24*PS:.1f}' font-weight='700' fill='#5A6270'> {fmt_ele(ele)} m</tspan>"
        o.append(f'<text x="{x+dx}" y="{y+dy+10*PS:.1f}" text-anchor="{anc}" font-size="{fs:.1f}" font-weight="800" '
                 f'fill="{col}" stroke="#FFFFFF" stroke-width="{7*PS:.1f}" paint-order="stroke" '
                 f'stroke-linejoin="round">{txt}</text>')

    # 指北针
    o.append(f'<g transform="translate({IW-76},78)">'
             f'<circle r="42" fill="#FFFFFF" opacity="0.92"/>'
             f'<path d="M 0,-30 L 11,12 L 0,5 L -11,12 Z" fill="#3A4250"/>'
             f'<text y="38" text-anchor="middle" font-size="20" font-weight="800" fill="#3A4250">N</text></g>')

    # 比例尺 2 km（放右下角，避开南端起点标注）
    bar = 2000 / _M_PER_PX
    bx, by = IW - 62 - bar, IH - 58
    o.append(f'<g><rect x="{bx-18}" y="{by-48}" width="{bar+36}" height="68" rx="10" fill="#FFFFFF" opacity="0.9"/>'
             f'<rect x="{bx}" y="{by-24}" width="{bar/2:.1f}" height="9" fill="#3A4250"/>'
             f'<rect x="{bx+bar/2:.1f}" y="{by-24}" width="{bar/2:.1f}" height="9" fill="#FFFFFF" stroke="#3A4250" stroke-width="1.5"/>'
             f'<text x="{bx}" y="{by-32}" font-size="19" font-weight="700" fill="#3A4250">0</text>'
             f'<text x="{bx+bar/2:.1f}" y="{by-32}" text-anchor="middle" font-size="19" fill="#3A4250">1</text>'
             f'<text x="{bx+bar:.1f}" y="{by-32}" text-anchor="end" font-size="19" font-weight="700" fill="#3A4250">2 km</text></g>')
    return "".join(o)


# 网页内嵌底图的最大宽度。底图栅格是超采样的（宽 = 逻辑宽 × ss = 3008），
# 全尺寸塞进 HTML 会让页面变成十几 MB。版式里地图占 730 CSS px（2× 屏 1460 设备像素），
# 内嵌到 1600 已经略高于实际显示像素，再多只是徒增体积（实测 2256 → 1600 可省一半）。
# 「放大要看细节」交给单独的高清地图（render_map_hi.py，它以 embed_max=0 取原生 3008）。
EMBED_MAX = 1600


def _embed_jpeg(max_w=EMBED_MAX):
    """读底图 → 需要时按最大宽度降采样 → base64。max_w=0 表示不降采样（取原生分辨率）。"""
    import io
    from PIL import Image
    im = Image.open(OUT / "base_map.jpg").convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=86, optimize=True, progressive=True, subsampling=0)
    return base64.b64encode(b.getvalue()).decode(), im.size


def build_svg(embed_image=True, embed_max=EMBED_MAX):
    img_tag = ""
    if embed_image:
        b64, isz = _embed_jpeg(embed_max)
        # 只写 href：同时写 xlink:href + href 会把同一份 base64 内嵌两遍，页面体积直接翻倍。
        # SVG2 的 href 在现代浏览器全支持，xlink 是历史遗留。
        img_tag = (f'<image x="0" y="0" width="{IW}" height="{IH}" '
                   f'href="data:image/jpeg;base64,{b64}"/>')
    return (f'<svg viewBox="0 0 {IW} {IH}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" aria-label="九华山南北穿越全线地图">'
            f'{img_tag}{build_overlay()}</svg>')


if __name__ == "__main__":
    svg = build_svg(embed_image=True)
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
            'body{margin:0;background:#EFEBE4}'
            '.w{width:1100px;margin:20px auto;background:#fff;border-radius:16px;padding:16px}'
            'svg{display:block;width:100%;height:auto}</style></head><body>'
            f'<div class="w">{svg}</div></body></html>')
    (OUT / "preview_map.html").write_text(page, encoding="utf-8")
    print("preview written; m/px=%.3f  base=%dx%d" % (_M_PER_PX, IW, IH))
