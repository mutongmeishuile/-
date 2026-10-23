# -*- coding: utf-8 -*-
"""通用攻略 HTML 生成器：route_def.py（数据）+ out/（自渲染底图）→ <线路>-攻略.html

设计原则（对应技能要求「根据技能自助生成攻略，不依赖已生成的文件」）：
  · **不做任何线路专属的硬编码** —— 线路名、分日色、文案、逐日卡、提示、来源声明
    全部从 route_def.CFG / DAY_CARDS / NOTES / SRC 读；文件里搜不到任何具体地名。
  · **数字尽量自动派生** —— 总里程、累计升降、最高点、用时、分段里程都由
    route_def 的数据算出来，避免"改了轨迹忘了改文案"。
  · 只依赖两样东西：route_def.py 与 out/base_meta.json + out/base_map.jpg
    （都由 make_terrain.py 产出）。删掉 out/ 重跑即可，不依赖任何"上次留下的成品文件"。

用法：
    python build_guide.py            # 出 <stem>-攻略.html
需要先跑：prep_kml_track.py（出 track_real.json / profile_real.json）→ make_terrain.py
"""
import bisect
import json
import re

import guide_common as GC
import map_svg as MS
import route_def as RD

if not getattr(RD, "TRACK_READY", True):
    raise SystemExit("轨迹数据未就绪：先跑 python prep_track.py <你的.kml>")

CFG = getattr(RD, "CFG", {})
_r = RD.build_route()
TOTAL_KM = _r["total_km"]
SPLIT_I = _r["split1"]
KM = _r["km"]

DAYS = CFG.get("days", [])
D1_C = DAYS[0]["color"] if DAYS else "#E4572E"
D2_C = DAYS[1]["color"] if len(DAYS) > 1 else "#1E9E76"
D1_T = DAYS[0].get("text_color", D1_C) if DAYS else "#B23A1C"
D2_T = DAYS[1].get("text_color", D2_C) if len(DAYS) > 1 else "#0F6E5C"
DAY_COLS = [d["color"] for d in DAYS] or [D1_C, D2_C]
DAY_TXT = [d.get("text_color", d["color"]) for d in DAYS] or [D1_T, D2_T]
STEM = CFG.get("file_stem", "线路")

def _find(name):
    for p in (GC.OUT / name, GC.HERE / name, GC.HERE / "kml" / name):
        if p.exists():
            return p
    raise FileNotFoundError(f"找不到 {name} —— 先跑 prep_kml_track.py")


PROF = json.loads(_find("profile_real.json").read_text(encoding="utf-8"))


def _auto_ele_range():
    lo, hi = min(p[1] for p in PROF), max(p[1] for p in PROF)
    return int(lo // 500 * 500), int(-(-hi // 500) * 500)


ELE_LO, ELE_HI = CFG.get("ele_range") or _auto_ele_range()


# ---------------------------------------------------------------- 剖面
def _prof_geom(fs=1.0):
    """剖面画布几何：profile_svg 与标注预检共用一套，避免两处各算一遍然后漂移。"""
    PW, PH = 1000, round(360 * (1 if fs == 1.0 else 1.06))
    pl = round(max(66, 46 * fs))
    pr = 976
    pt = round(48 * fs) + 26
    pb = PH - round(58 * fs) - 16
    kx = (pr - pl) / TOTAL_KM
    ky = (pb - pt) / (ELE_HI - ELE_LO)
    return {"PW": PW, "PH": PH, "pl": pl, "pr": pr, "pt": pt, "pb": pb, "kx": kx, "ky": ky}


def check_prof_labels(fs=1.0, mobile=False):
    """剖面标注密度预检：**渲染前**就算出"哪两条标注会叠字"，别再靠看图发现。

    为什么要有（2026-10-23 大横断海子线实测，改了三轮才对）
    ------------------------------------------------------
    剖面横轴 (pr-pl)/TOTAL_KM ≈ 12.3 viewBox px/km，而一条标签有 90–150 px 宽 ——
    **相邻 8 km 以内的两条标注，同侧摆必然叠字**。那次的实际翻车：
      · 黑海子(41.4)/白海子(43.7)/石头垭口(45.6)/长海子(47.2) 四点在 6 km 内，
        叠成「黑海子 4635长海子 4680」一行，来回改了三轮；
      · 终点 2416 m 贴着绘图区底边，BOT_LBL 的 +38 px 固定偏移把字压到横轴刻度上
        （渲染成「65终点 各坝村 2416」）。
    这两类都能用 text_w 估宽 + off 估高、两两判 AABB 相交一次算出。

    只做提示、不改任何东西；返回 [(说明, 建议)] 供 build_html 打印。
    """
    g = _prof_geom(fs)
    fsz_of = lambda hi: round((13 if hi else 12) * fs)          # noqa: E731
    boxes = []

    marks = RD.MARKS
    if mobile:
        marks = [m for m in marks if len(m) < 6 or m[5]]
    for mk in marks:
        km, el, txt, hi, off = mk[0], mk[1], mk[2], mk[3], mk[4]
        fsz = fsz_of(hi)
        w = GC.text_w(txt, fsz)
        # 与渲染一致：文字居中，横向夹在绘图区内
        tx = min(max(g["pl"] + km * g["kx"], g["pl"] + w / 2 + round(2 * fs)),
                 g["pr"] - w / 2 - round(2 * fs))
        y = g["pb"] - (el - ELE_LO) * g["ky"] + off * fs
        boxes.append({"txt": txt, "x0": tx - w / 2, "x1": tx + w / 2,
                      "y0": y - fsz * 0.86, "y1": y + fsz * 0.30,
                      "km": km, "off": off, "kind": "mark"})
    for km, el, txt, anc in RD.BOT_LBL:
        fsz = round(12 * fs)
        w = GC.text_w(txt, fsz)
        x = g["pl"] + km * g["kx"]
        if anc == "start":
            x0 = x + round(9 * fs)
        elif anc == "end":
            x0 = x - round(6 * fs) - w
        else:
            x0 = x - w / 2
        y = g["pb"] - (el - ELE_LO) * g["ky"] + round(38 * fs)
        boxes.append({"txt": txt, "x0": x0, "x1": x0 + w,
                      "y0": y - fsz * 0.86, "y1": y + fsz * 0.30,
                      "km": km, "off": 0, "kind": "bot"})

    out = []
    for i in range(len(boxes)):
        a = boxes[i]
        # ① 掉出画布 / 压横轴刻度：BOT_LBL 的固定 +38 px 是元凶
        if a["y1"] > g["PH"]:
            out.append((f"「{a['txt']}」超出画布下沿 {a['y1'] - g['PH']:.0f} px → 会被裁掉",
                        "把该标注从 BOT_LBL 挪到 MARKS（纵偏移可调）"))
        elif a["kind"] == "bot" and a["y0"] > g["pb"] - 2:
            # 文字"起点"落在横轴线**以下**才算压刻度带（线以上、区内的属正常位置，
            # 2026-10-23 首版判据写成 y0 < pb+24，把珠峰东坡/萨普的正常标注全报成错）
            out.append((f"「{a['txt']}」落到横轴线以下（文字顶 y={a['y0']:.0f} / 轴 y={g['pb']:.0f}），"
                        f"会压住刻度数字",
                        "BOT_LBL 的文字固定在点位下方 38 px；海拔贴近 ELE_LO 时改走 MARKS"))
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            ox = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            oy = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            # ⚠ 判据是"够不够读"，不是"有没有面积相交"：两块只差 0–10 px 时人眼看是
            #   连成一串（实测「黑海子 4635长海子 4680」就是 ox≈0 的情形），必须一并报。
            if ox > -10 and oy > -5:
                dkm = abs(a["km"] - b["km"])
                same_side = (a["off"] >= 0) == (b["off"] >= 0)
                head = (f"「{a['txt']}」×「{b['txt']}」" +
                        (f"叠字 {ox:.0f}×{oy:.0f} px" if ox > 0 and oy > 0
                         else f"只差 {max(ox, 0):.0f} px 宽 / {oy:.0f} px 高，读起来会连成一串"))
                if same_side:
                    tip = ("两点隔 %.1f km（%.0f px），标签宽 %.0f px —— "
                           "把其中一个的 off 改成另一侧（正=曲线下方，负=上方）"
                           % (dkm, dkm * g["kx"], a["x1"] - a["x0"]))
                else:
                    tip = f"两点只隔 {dkm:.1f} km，一上一下仍不够 → 删掉次要的那个标注"
                out.append((head, tip))
    return out


def _report_prof_labels():
    """打印剖面标注预检（桌面版与手机版各一遍，只报不复述同一句）。"""
    for fs, mobile, tag in ((1.0, False, "桌面"), (2.4, True, "手机")):
        bad = check_prof_labels(fs, mobile)
        if not bad:
            print(f"   标注预检（{tag}版）：无叠字 ✓")
            continue
        print(f"   标注预检（{tag}版）：{len(bad)} 处会叠字 —— 现在是**算**出来的，不用等出图：")
        for what, tip in bad:
            print(f"     · {what}")
            print(f"       → {tip}")


def profile_svg(fs=1.0, mobile=False):
    _g = _prof_geom(fs)
    PW, PH, pl, pr, pt, pb = _g["PW"], _g["PH"], _g["pl"], _g["pr"], _g["pt"], _g["pb"]
    kx, ky = _g["kx"], _g["ky"]

    def X(km):
        return pl + km * kx

    def Y(m):
        return pb - (m - ELE_LO) * ky

    o = []
    m = ELE_LO
    while m <= ELE_HI:
        o.append(f'<line x1="{pl}" y1="{Y(m):.1f}" x2="{pr}" y2="{Y(m):.1f}" '
                 f'stroke="#E9E3D9" stroke-width="1"/>')
        o.append(f'<text x="{pl-round(10*fs)}" y="{Y(m)+round(4*fs):.1f}" text-anchor="end" '
                 f'font-size="{round(11.5*fs)}" fill="#68717E">{m}</text>')
        m += 500
    for km in range(0, int(TOTAL_KM) + 1, 5):
        o.append(f'<line x1="{X(km):.1f}" y1="{pt-8}" x2="{X(km):.1f}" y2="{pb}" '
                 f'stroke="#EDE7DD" stroke-width="1"/>')
        o.append(f'<text x="{X(km):.1f}" y="{pb+round(18*fs)}" text-anchor="middle" '
                 f'font-size="{round(11.5*fs)}" fill="#68717E">{km}</text>')
    o.append(f'<line x1="{pl}" y1="{pb}" x2="{pr}" y2="{pb}" stroke="#CFC6B8" stroke-width="1.2"/>')
    o.append(f'<text x="{(pl+pr)/2:.0f}" y="{pb+round(38*fs)}" text-anchor="middle" '
             f'font-size="{round(12*fs)}" fill="#5F6875">累计里程 (km)</text>')
    o.append(f'<text x="{round(12*fs)}" y="{(pt+pb)/2:.0f}" font-size="{round(12*fs)}" '
             f'fill="#5F6875" transform="rotate(-90 {round(12*fs)} {(pt+pb)/2:.0f})" '
             f'text-anchor="middle">海拔 (m)</text>')

    bounds = [0.0] + [float(b) for b in CFG.get("day_bounds", [KM[SPLIT_I]])] + [TOTAL_KM]
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        col = DAY_COLS[i % len(DAY_COLS)]
        colt = DAY_TXT[i % len(DAY_TXT)]
        seg = [p for p in PROF if a <= p[0] <= b]
        if not seg:
            continue
        if seg[0][0] > a:
            seg = [(a, seg[0][1])] + seg
        if seg[-1][0] < b:
            seg = seg + [(b, seg[-1][1])]
        d = "M " + " L ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in seg)
        o.append(f'<path d="{d} L {X(seg[-1][0]):.1f},{pb} L {X(seg[0][0]):.1f},{pb} Z" '
                 f'fill="{col}" opacity="0.16"/>')
        o.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.6" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
        tag = DAYS[i]["tag"] if i < len(DAYS) else f"D{i+1}"
        o.append(f'<text x="{(X(a)+X(b))/2:.1f}" y="{pt-round(16*fs)}" text-anchor="middle" '
                 f'font-size="{round(14*fs)}" font-weight="800" fill="{colt}">{tag}</text>')

    for b in CFG.get("day_bounds", [KM[SPLIT_I]]):
        o.append(f'<line x1="{X(b):.1f}" y1="{pt-4}" x2="{X(b):.1f}" y2="{pb}" '
                 f'stroke="#B9B2A6" stroke-width="1.1" stroke-dasharray="5 4"/>')

    marks = RD.MARKS
    if mobile:
        marks = [m for m in marks if len(m) < 6 or m[5]]
    for mk in marks:
        km, el, txt, hi, off = mk[0], mk[1], mk[2], mk[3], mk[4]
        o.append(f'<circle cx="{X(km):.1f}" cy="{Y(el):.1f}" '
                 f'r="{(5.0 if hi else 3.6)*min(fs,1.6):.1f}" '
                 f'fill="{D1_T if hi else "#3A4250"}" stroke="#FFFFFF" stroke-width="1.6"/>')
        # 标注文字**夹在绘图区内**：起终点正好落在 km=0 / km=TOTAL 上，居中锚点会让
        # 半行字跑到画布外（2026-10-23 博格达实测：右端「黑沟桥 2519」被裁成「黑沟桥 251」）。
        _mfs = round((13 if hi else 12) * fs)
        _tw = GC.text_w(txt, _mfs)
        _tx = min(max(X(km), pl + _tw / 2 + round(2 * fs)), pr - _tw / 2 - round(2 * fs))
        o.append(f'<text x="{_tx:.1f}" y="{Y(el)+off*fs:.1f}" text-anchor="middle" '
                 f'font-size="{_mfs}" font-weight="800" '
                 f'fill="{D1_T if hi else "#3A4250"}" stroke="#FFFFFF" '
                 f'stroke-width="{round(3.2*fs)}" paint-order="stroke" '
                 f'stroke-linejoin="round">{txt}</text>')

    for km, el, txt, anc in RD.BOT_LBL:
        x, y = X(km), Y(el)
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="#FFFFFF" stroke="#3A4250" '
                 f'stroke-width="2"/>')
        if anc == "start":
            ty, tx, a2 = y + round(38 * fs), x + round(9 * fs), "start"
        elif anc == "end":
            ty, tx, a2 = y + round(38 * fs), x - round(6 * fs), "end"
        else:
            ty, tx, a2 = y + round(20 * fs), x, "middle"
        o.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{a2}" '
                 f'font-size="{round(12*fs)}" font-weight="700" fill="#3A4250" stroke="#FFFFFF" '
                 f'stroke-width="{round(3.2*fs)}" paint-order="stroke" '
                 f'stroke-linejoin="round">{txt}</text>')

    return (f'<svg class="profsvg" viewBox="0 0 {PW} {PH}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" aria-label="{CFG.get("aria_prof", "全程海拔剖面")}">'
            f'<rect x="0" y="0" width="{PW}" height="{PH}" fill="#FFFFFF" rx="14"/>'
            f'<g>{"".join(o)}</g></svg>')


# ---------------------------------------------------------------- 逐日卡
def day_card(i, tag, route, dist, gain, dur, top, water, camp, note,
             campkey=None, waterkey=None):
    col = DAY_COLS[(i - 1) % len(DAY_COLS)]
    colt = DAY_TXT[(i - 1) % len(DAY_TXT)]
    campkey = campkey or CFG.get("camp_key", "住宿")
    waterkey = waterkey or CFG.get("water_key", "水源")
    return f'''<article class="day" style="--c:{col};--ct:{colt}">
      <div class="day-head"><span class="dtag">{tag}</span><span class="droute">{route}</span></div>
      <div class="drow">
        <span class="dnum">{dist}</span><span class="dsep">|</span>
        <span class="dnum">{gain}</span><span class="dsep">|</span>
        <span class="dnum">{dur}</span><span class="dsep dsep2">|</span>
        <span class="dtop">{top}</span>
      </div>
      <div class="dline"><span class="dkey">{waterkey}</span><span class="dval">{water}</span></div>
      <div class="dline"><span class="dkey">{campkey}</span><span class="dval">{camp}</span></div>
      <div class="dnote">{note}</div>
    </article>'''


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#EFEBE4;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif;color:#1F2430;-webkit-font-smoothing:antialiased}
.page{width:1120px;margin:0 auto;padding:26px 26px 34px;background:#FBF8F4}
.hd{display:flex;align-items:center;gap:12px;padding:2px 2px 10px}
.hd .badge{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:700;color:#0F6E5C;background:#E4F2EE;border:1px solid #CBE6DF;border-radius:999px;padding:5px 12px}
.hd .badge i{width:8px;height:8px;background:#0F6E5C;transform:rotate(45deg);display:inline-block;border-radius:1.5px}
.hd .meta{margin-left:auto;font-size:12.5px;color:#68717E;letter-spacing:.4px}
h1{margin:6px 2px 4px;font-size:39px;line-height:1.15;letter-spacing:-.5px}
h1 .lite{font-size:19px;color:#5A6270;font-weight:600;letter-spacing:0}
.sub{margin:0 2px 18px;font-size:15.5px;font-weight:600;color:#5A6270;line-height:1.6}
.sub em{font-style:normal;color:#0F6E5C}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat{background:#fff;border:1px solid #EAE3D9;border-radius:14px;padding:16px 18px 14px;box-shadow:0 1px 2px rgba(31,36,48,.03)}
.stat b{display:block;font-size:25px;letter-spacing:-.4px;line-height:1.2}
.stat span{display:block;margin-top:5px;font-size:12.5px;color:#68717E;font-weight:600}
.panel{background:#fff;border:1px solid #EAE3D9;border-radius:16px;padding:16px 18px 18px;margin-bottom:20px;box-shadow:0 1px 2px rgba(31,36,48,.03)}
.ph{display:flex;align-items:baseline;gap:10px;margin:0 2px 12px}
.ph h2{margin:0;font-size:19px;letter-spacing:.2px}
.ph .tagn{font-size:12.5px;color:#68717E;font-weight:600;letter-spacing:.5px}
.ph .right{margin-left:auto;font-size:13px;color:#B23A1C;font-weight:700}
.ph .right.n{color:#68717E;font-weight:600}
svg{display:block;width:100%;height:auto}
.rbox-lgm{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px;padding-top:14px;border-top:1px dashed #E7E0D6}
/* 附加行（雪线 / 步道）说明文字长：整行铺满，避免在 1/3 格里把 tag 挤成两行 */
.rbox-lgm .lg.wide{grid-column:1/-1}
.rbox-lgm .lg.wide i{width:34px}
.rbox-lgm .lg b{white-space:nowrap}
.lg{display:flex;align-items:center;gap:9px;font-size:13px;color:#3A4250}
.lg i{width:22px;height:5px;border-radius:3px;flex:0 0 auto}
.lg b{font-weight:800}
.lg span{color:#68717E;font-size:12px;margin-left:2px}
.days{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:20px}
.day{background:#fff;border:1px solid #EAE3D9;border-radius:14px;padding:15px 18px 16px;border-top:4px solid var(--c);box-shadow:0 1px 2px rgba(31,36,48,.03);display:flex;flex-direction:column}
.day-head{display:flex;align-items:baseline;gap:10px;margin-bottom:9px}
.dtag{font-size:21px;font-weight:900;color:var(--ct);letter-spacing:-.3px;flex:0 0 auto}
.droute{font-size:15.5px;font-weight:700}
.drow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px;margin-bottom:10px}
.dnum{font-weight:800;color:#2A3140}
.dsep{color:#C9BFAE}
.dtop{color:#B23A1C;font-weight:800}
.dline{display:flex;gap:9px;font-size:13px;line-height:1.64;margin-bottom:4px}
.dkey{flex:0 0 34px;font-weight:800;color:#0F6E5C}
.dval{color:#4A5261;flex:1}
.dnote{margin-top:auto;font-size:12.6px;line-height:1.66;color:#7A6357;background:#FBF3EC;border:1px solid #F1E2D5;border-radius:8px;padding:8px 11px}
table.tsc{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
table.tsc th{text-align:left;font-size:12px;color:#5F6875;font-weight:700;padding:0 8px 7px;border-bottom:1px solid #E7E0D6}
table.tsc td{padding:6.5px 8px;border-bottom:1px solid #F1ECE4;color:#3A4250}
table.tsc td.n{font-weight:800;color:#1F2430}
table.tsc td.num{font-variant-numeric:tabular-nums}
table.tsc tr:last-child td{border-bottom:none}
table.tsc tr.d2 td{background:#F6FAF8}
.notes{background:#fff;border:1px solid #EAE3D9;border-radius:16px;padding:16px 18px 18px;margin-bottom:20px}
.ngrid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.nbox h3{margin:0 0 8px;font-size:14.5px;color:#0F6E5C;letter-spacing:.2px}
.nbox ul{margin:0;padding-left:0;list-style:none}
.nbox li{font-size:12.8px;line-height:1.74;color:#4A5261;padding-left:14px;position:relative;margin-bottom:2px}
.nbox li:before{content:"";position:absolute;left:2px;top:9px;width:5px;height:5px;border-radius:50%;background:#C9D8D3}
.src{display:grid;grid-template-columns:1fr 1fr;gap:18px;font-size:12.4px;line-height:1.78;color:#5A6270}
.src h3{margin:0 0 6px;font-size:13px;color:#3A4250}
.src p{margin:0 0 6px}
.src .k{color:#68717E;font-weight:700}
.foot{font-size:12px;color:#68717E;line-height:1.7;padding:0 4px;margin-top:14px}
.foot b{color:#4A5261}
.nb{white-space:nowrap}
.prof-p{display:none}
.map-p{display:none}
.stat span.monly{display:none}
"""

MOBILE_CSS = """
@media (max-width:900px){
  body{background:#FBF8F4}
  .page{width:100%;max-width:430px;padding:16px 15px 24px}
  .hd{flex-wrap:wrap;gap:8px;padding-bottom:8px}
  .hd .meta{margin-left:0;width:100%;font-size:12.5px;line-height:1.5}
  h1{font-size:28px;line-height:1.25}
  h1 .lite{font-size:15.5px}
  .sub{font-size:14.5px;line-height:1.7;margin-bottom:16px}
  .stats{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
  .stat{padding:12px 14px 11px}
  .stat b{font-size:19px}
  .stat span{font-size:12.2px}
  .stat span.donly{display:none}
  .stat span.monly{display:block}
  .panel{border-radius:14px;padding:14px 15px 16px;margin-bottom:16px}
  .ph{flex-wrap:wrap;gap:6px 10px}
  .ph h2{font-size:18px}
  .ph .right{margin-left:0;width:100%;font-size:12.5px}
  .days{grid-template-columns:1fr;gap:12px}
  .day{padding:14px 15px 15px}
  .day-head{flex-wrap:wrap;gap:4px 10px}
  .dtag{font-size:22px}
  .droute{font-size:15.5px}
  .drow{font-size:14.6px}
  .drow>.dsep2{display:none}
  .drow>.dtop{flex:0 0 100%}
  .dline{font-size:14.2px;line-height:1.7}
  .dkey{flex:0 0 38px}
  .dnote{font-size:13.6px;padding:8px 11px}
  .ngrid{grid-template-columns:1fr;gap:15px}
  .nbox h3{font-size:15.5px}
  .nbox li{font-size:14.2px;line-height:1.8}
  .rbox-lgm{display:grid;grid-template-columns:1fr;gap:9px}
  .lg{font-size:14px}
  .lg span{font-size:13.2px}
  table.tsc{font-size:12.6px}
  table.tsc th{font-size:11.6px}
  table.tsc td{padding:6px 4px}
  .src{grid-template-columns:1fr;gap:14px;font-size:13px;line-height:1.8}
  .foot{font-size:12.8px;line-height:1.8}
  .prof-d{display:none}
  .prof-p{display:block}
  .map-d{display:none}
  .map-p{display:block}
  .mapsvg .il{display:none}
}
"""


def nb(s):
    """给"数字+单位"加不可断行包裹，避免折行时单位被甩到下一行。"""
    return re.sub(r"(\d+(?:\.\d+)?)\s*(km|m|min|h|℃|元|人|%)", r'<span class="nb">\1 \2</span>', s)


# ---------------------------------------------------------------- 自动派生
def _duration():
    if CFG.get("duration"):
        return CFG["duration"]
    if len(RD.SCHEDULE) >= 2:
        _t = RD.SCHEDULE[-1][3]
        if _t and _t not in ("—", "-", ""):
            return f"{_t} 收队"
    return "—"


def _auto_stats():
    _with_ele = [d for d in GC.pois_of(RD) if d["ele"] is not None]
    top = max(_with_ele, key=lambda d: d["ele"]) if _with_ele else None
    top_name = top["name"].split(" · ")[0] if top else "最高点"
    dur = _duration()
    cards = [
        (f"{TOTAL_KM:.1f} km", "总里程（实测轨迹累计）", "总里程 · 实测轨迹"),
        (f"+{RD.ASC} / −{RD.DESC} m", "累计爬升 / 下降", "累计爬升 / 下降"),
        (f"{RD.fmt_ele(top['ele'])} m" if top else "—",
         f"最高点 · {top_name}", f"最高点 · {top_name}"),
        (dur, "实测用时", "实测用时"),
    ]
    return CFG.get("stats", cards)


def _stats_html():
    out = []
    for a, b, c in _auto_stats():
        out.append(f'<div class="stat"><b>{a}</b><span class="donly">{b}</span>'
                   f'<span class="monly">{c}</span></div>')
    return "".join(out)


def _legend_html():
    rows = []
    for d in DAYS:
        desc = str(d.get("legend", "") or "").strip()
        # 与 map_svg._legend_rows 同一处理：CFG 的 legend 常自带「D1 」前缀，
        # 而前面已经有一列 <b>{tag}</b> —— 不剔就印成「D1 D1 鸡丑垭口 → 两河口营地」。
        if desc.startswith(d["tag"] + " "):
            desc = desc[len(d["tag"]) + 1:]
        rows.append((d["color"], d["tag"], desc, d.get("dist_label", "")))
    # 附加行与 map_svg._legend_rows 同源：CFG["legend_extra"] 列表优先
    # （雪线 / 步道 / 冰川面可并存），缺省退回旧的单行 extra_legend_* 键。
    # ⚠ 2026-10-21 珠峰东坡实测：旧版这里只认 extra_legend_desc，
    #   而 map_svg 认 legend_extra —— 结果**地图内有雪线/步道两行、HTML 图例没有**，
    #   两套图例对不上（"图例要覆盖图上所有符号"直接失效）。
    extra = CFG.get("legend_extra")
    if extra:
        for it in extra:
            rows.append((it.get("color", "#8C8C86"), it.get("tag", ""),
                         it.get("desc", ""), it.get("dist_label", ""),
                         bool(it.get("dash"))))
    elif CFG.get("extra_legend_desc"):
        rows.append((CFG.get("extra_legend_color", "#8C8A86"),
                     CFG.get("extra_legend_tag", "步道"), CFG["extra_legend_desc"], "", True))
    out = []
    for r in rows:
        c, t, d, k = r[0], r[1], r[2], r[3]
        dashed = len(r) > 4 and r[4]
        # 虚线行（雪线 / 步道）样本画成虚线，别用实心色条冒充
        style = (f"background:repeating-linear-gradient(90deg,{c} 0 5px,transparent 5px 9px)"
                 if dashed else f"background:{c}")
        out.append(f'<div class="lg{" wide" if dashed else ""}"><i style="{style}"></i><b>{t}</b> {d}'
                   f'<span>{k}</span></div>')
    return "".join(out)


# ---- 累计升降（与 prep_kml_track 同口径：11 点平滑 + 3 m 阈值）----
_CUM_KM = _CUM_ASC = _CUM_DESC = None


def _load_cum():
    """从 track_full.json 建前缀累计升降表（只读一次）。"""
    global _CUM_KM, _CUM_ASC, _CUM_DESC
    if _CUM_KM is not None:
        return
    try:
        d = json.loads((GC.OUT / "track_full.json").read_text(encoding="utf-8"))
        pts, cum = d["pts"], d["cum_m"]
    except Exception:                                          # noqa
        _CUM_KM, _CUM_ASC, _CUM_DESC = [], [], []
        return
    ele = [p[2] for p in pts]
    n = len(ele)
    # 11 点滑动平均（前缀和 O(n)）
    pref = [0.0] * (n + 1)
    for i, v in enumerate(ele):
        pref[i + 1] = pref[i] + v
    sm = [0.0] * n
    for i in range(n):
        a, b = max(0, i - 5), min(n, i + 6)
        sm[i] = (pref[b] - pref[a]) / (b - a)
    asc = [0.0] * n
    desc = [0.0] * n
    prev = sm[0]
    ua = ud = 0.0
    for i in range(1, n):
        dd = sm[i] - prev
        if abs(dd) >= 3.0:
            if dd > 0:
                ua += dd
            else:
                ud -= dd
            prev = sm[i]
        asc[i] = ua
        desc[i] = ud
    _CUM_KM = [c / 1000.0 for c in cum]
    _CUM_ASC, _CUM_DESC = asc, desc


def _cum_at(km):
    _load_cum()
    if not _CUM_KM:
        return 0, 0
    i = bisect.bisect_left(_CUM_KM, km)
    i = min(i, len(_CUM_KM) - 1)
    return int(round(_CUM_ASC[i])), int(round(_CUM_DESC[i]))


def _parse_hhmm(s):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(s or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _fmt_dur(mins):
    if mins is None:
        return "—"
    h, m = divmod(int(mins), 60)
    return f"{h}h{m:02d}m" if h else f"{m}min"


def schedule_rows():
    bounds = CFG.get("day_bounds", [KM[SPLIT_I]])
    out = []
    prev_min = None
    for name, km, ele, t, note in RD.SCHEDULE:
        cls = ' class="d2"' if any(km > b for b in bounds) else ''
        a, d = _cum_at(km)
        cur = _parse_hhmm(t)
        dur = "—"
        if cur is not None and prev_min is not None:
            delta = cur - prev_min
            dur = "过夜" if delta < 0 else _fmt_dur(delta)
        if cur is not None:
            prev_min = cur
        out.append(f'<tr{cls}><td class="n">{name}</td><td class="num">{km:.2f}</td>'
                   f'<td class="num">{int(round(ele))}</td>'
                   f'<td class="num">+{a}</td><td class="num">{d}</td>'
                   f'<td class="num">{t}</td><td class="num">{dur}</td>'
                   f'<td>{note}</td></tr>')
    return "".join(out)


def _legend_css():
    """图例画在图面内时，桌面端不要再显示一份页内文字图例。

    2026-10-23 实测：`legend_outside` 关掉（图例卡画在图上）之后，地图下方那条
    `.rbox-lgm` 仍然照渲 —— 同一份 D1–D7 图例出现两遍，页面看着很啰嗦。
    页内文字图例本来是给**手机**用的（图面内图例在手机上会缩到读不出），
    所以这里只在桌面隐藏，手机端由 MOBILE_CSS 重新打开。
    开关仍是 `legend_outside`：打开时图面内没有卡，页内图例就是唯一图例，不能隐藏。
    """
    place = str(CFG.get("legend_place") or "").strip().lower()
    if place not in ("map", "band", "title"):
        # 默认 "title"（2026-10-23 起）：地图上没有图例卡，页内文字图例就是唯一图例
        place = "band" if CFG.get("legend_outside") else "title"
    if place != "map":
        return ""
    return ".rbox-lgm{display:none}"


def _notes_html():
    boxes = []
    for title, items in CFG.get("notes", []):
        lis = "".join(f"<li>{x}</li>" for x in items)
        boxes.append(f'<div class="nbox"><h3>{title}</h3><ul>{lis}</ul></div>')
    return "".join(boxes)


def _src_html():
    cols = []
    for title, paras in CFG.get("src", []):
        ps = "".join(f"<p>{p}</p>" for p in paras)
        cols.append(f"<div><h3>{title}</h3>{ps}</div>")
    return f'<div class="src">{"".join(cols)}</div>' if cols else ""


def build_html():
    map_d, _ = MS.build_svg(mobile=False)
    c1 = MS.self_check(False)
    map_p, _ = MS.build_svg(mobile=True, embed_max=820)
    c2 = MS.self_check(True)
    _report_prof_labels()
    prof_d = profile_svg(fs=1.0, mobile=False)
    prof_p = profile_svg(fs=2.4, mobile=True)
    days_html = "".join(day_card(*c) for c in CFG.get("day_cards", []))
    src_html = _src_html()
    src_section = f'<section class="panel">{src_html}</section>' if src_html else ''
    return c1 + c2, f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{CFG.get("page_title", STEM + " · 攻略")}</title>
<style>{CSS}{_legend_css()}{MOBILE_CSS}</style>
</head>
<body>
<div class="page">
  <div class="hd">
    <span class="badge"><i></i>{CFG.get("badge", "")}</span>
    <span class="meta">{CFG.get("meta", "")}</span>
  </div>
  <h1>{CFG.get("title", STEM)} <span class="lite">{CFG.get("title_lite", "")}</span></h1>
  <p class="sub">{CFG.get("sub", "")}</p>

  <div class="stats">{_stats_html()}</div>

  <section class="panel">
    <div class="ph"><h2>全线地图</h2><span class="tagn">{CFG.get("map_tag", "路线为实测轨迹 · 非导航用图")}</span>
      <span class="right n">{CFG.get("map_right", "北为上")}</span></div>
    {map_d}{map_p}
    <div class="rbox-lgm">{_legend_html()}</div>
    <div class="foot" style="margin-top:10px;padding:0 2px">{CFG.get("map_note", "")}</div>
  </section>

  <section class="panel">
    <div class="ph"><h2>全程海拔剖面</h2>
      <span class="right">{CFG.get("prof_right", "")}</span></div>
    <div class="prof-d">{prof_d}</div>
    <div class="prof-p">{prof_p}</div>
    <div class="foot" style="margin-top:10px;padding:0 2px">{CFG.get("prof_note", "")}</div>
  </section>

  <section class="ph" style="margin:0 2px 10px"><h2>逐日攻略</h2>
    <span class="tagn">{CFG.get("days_tag", "里程 / 爬升 / 实测用时 / 水源 / 住宿")}</span></section>
  <div class="days">{days_html}</div>

  <section class="panel">
    <div class="ph" style="margin-bottom:12px"><h2>关键点时间表</h2>
      <span class="tagn">{CFG.get("sched_tag", "时刻取自轨迹时间戳")}</span></div>
    <table class="tsc">
      <colgroup><col style="width:17%"><col style="width:8%"><col style="width:8%">
        <col style="width:9%"><col style="width:9%"><col style="width:8%"><col style="width:9%"><col></colgroup>
      <thead><tr><th>点位</th><th>里程 km</th><th>海拔 m</th>
        <th>累计↑ m</th><th>累计↓ m</th><th>时刻</th><th>用时</th><th>说明</th></tr></thead>
      <tbody>{schedule_rows()}</tbody>
    </table>
  </section>

  <section class="notes">
    <div class="ph" style="margin-bottom:12px"><h2>关键提示</h2>
      <span class="tagn">{CFG.get("notes_tag", "")}</span></div>
    <div class="ngrid">{_notes_html()}</div>
  </section>

  {src_section}

  <div class="foot">{CFG.get("foot", "")}</div>
</div>
</body>
</html>'''


if __name__ == "__main__":
    checks, html = build_html()
    p = GC.ROOT / f"{STEM}-攻略.html"
    p.write_text(html, encoding="utf-8")
    print(f"written: {p}  {p.stat().st_size/1048576:.2f} MB")
    print(f"地图 {MS.IW}x{MS.IH}  比例尺 1 px = {MS.M_PER_PX:.2f} m")
    raise SystemExit(1 if checks else 0)
