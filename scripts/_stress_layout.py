"""受压测试（dev-only，`_` 前缀 = 不参与交付链）：确认「标注字号动态缩小 /
位置动态避让 / 图例动态缩小」这三套动态机制**真的会触发**。

为什么需要它：瘦高画幅的线路（如 143 km 南北向，逻辑画幅 1504×3446）上，
25 个标注全部命中档位 1.00、area=0 —— 动态缩小路径一次都没走到。
**机制写对了却从没被执行，和没写是一样的。** 每次改 `_layout` / `FS_LADDER` /
`_legend_scale` 后跑一遍：

    python scripts/_stress_layout.py        # 退出码非 0 = 有未通过项

关键：字号阶梯是**兜底**。`_layout` 在每个字号下都会先做位置搜索，
所以光"周围有东西"不够（它会绕开）—— 必须让**全画幅没有任何位置放得下满字号**。
做法是横向阻塞条带（挡 16 / 空 16）：
  满字号框高 ≈ 24.6 px > 16  → 任何窗口都必然压到阻挡条；
  0.6 档框高 ≈ 14.7 px < 16  → 能整个落进空隙。

只动内存里的模块全局（IW/IH/_OCC/_LABELS），不写任何交付文件。
"""
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 预置 route_def stub：本测试只用 `_layout` / `_legend_*`，不碰轨迹数据。
# 技能仓库的模板 route_def 是 TRACK_READY=False（还没跑 prep_track），
# 直接 import map_svg 会被它 SystemExit 掉。
_stub = types.ModuleType("route_def")
_stub.TRACK_READY = True
_stub.CFG = {}
sys.modules["route_def"] = _stub

import guide_common as GC                 # noqa: E402

# 同理 stub 掉 load_meta：本测试只需要一个能算出 IW/IH/PROJ 的最小 meta，
# 画幅随后由 reset() 覆写，这些值不影响任何断言。
_FAKE_META = {"size": [1504, 3446], "tx0": 0, "ty0": 0, "box": [0, 0, 0, 0],
              "scale": 1, "z": 13, "bbox": [84.0, 43.0, 85.0, 44.0]}
GC.load_meta = lambda: dict(_FAKE_META)

import map_svg as MS                      # noqa: E402

FS_LADDER = (1.0, 0.92, 0.84, 0.76, 0.68, 0.6)
TXT = "D3 冰川湖营地 3520 m"      # 样本串：长度接近真实标注即可
BASE = 21 * MS.PS_BASE


def reset(w, h):
    MS.IW, MS.IH = w, h
    MS._OCC.clear()
    MS._LABELS.clear()


def stripes(w, h, period=32, thick=16):
    """横向阻塞条带：挡 thick / 空 (period-thick)，铺满宽度。"""
    y = 0.0
    while y < h:
        MS._OCC.append((0.0, y, float(w), y + thick))
        y += period


def ladder_probe(x, y, dx, dy, anc, M=1.0, trace=False):
    """复刻 build_map_overlay 里的字号阶梯，返回 (档位, area, box, fs, 每档面积)。"""
    best = None
    rows = []
    for sc in FS_LADDER:
        fs = BASE * sc
        w = GC.text_w(TXT, fs) + 26 * M
        r = MS._layout(x, y, dx * M, dy * M, anc, w, fs * 0.86, commit=False)
        rows.append((sc, r[4]))
        if best is None or r[4] < best[4] - 1e-6 or \
           (abs(r[4] - best[4]) <= 1e-6 and sc > best[5]):
            best = r + (sc,)
        if r[4] == 0:
            break
    if trace:
        for sc, a in rows:
            print(f"        档位 {sc:.2f}  area {a:8.0f}{' ← 选中' if sc == best[5] else ''}")
    return best[5], best[4], best[3], BASE * best[5], rows


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"    {'✓' if cond else '✗'} {label}{('  ' + detail) if detail else ''}")


print("=" * 70)
print("受压测试 1：字号阶梯 —— 满字号放不下时必须往小档走")
print("=" * 70)

reset(1504, 3446)                                   # 对照：瘦高线路的真实画幅
sc, area, _, fs, _ = ladder_probe(752, 1700, 14, -10, "start")
print(f"  A 对照 1504×3446（空画布）   → 档位 {sc:.2f}  area {area:6.0f}  字 {fs:.1f}px")
check("宽裕时不缩字（位置搜索够用）", sc == 1.0 and area == 0)

reset(1504, 430)
stripes(1504, 430)
sc, area, box, fs, rows = ladder_probe(752, 215, 14, -10, "start", trace=True)
print(f"  B 条带阻塞 1504×430          → 档位 {sc:.2f}  area {area:6.0f}  字 {fs:.1f}px")
check("满字号放不下 → 自动降档", sc < 1.0, f"选中 {sc:.2f}")
check("重叠随降档单调不增（每档都是当时最优）",
      all(rows[i][1] >= rows[i + 1][1] - 1e-6 for i in range(len(rows) - 1)),
      " → ".join(f"{s:.2f}:{a:.0f}" for s, a in rows))
check("降档后重叠显著小于满字号", area < rows[0][1] * 0.5, f"{rows[0][1]:.0f} → {area:.0f}")

# 条带更密：连最小档也放不进整隙 → 应落到最小档并接受最小重叠
reset(1504, 430)
MS._OCC.extend((0.0, y, 1504.0, y + 20) for y in range(0, 430, 24))   # 挡20/空4
sc, area, box, fs, rows = ladder_probe(752, 215, 14, -10, "start")
print(f"  C 极密阻塞（挡20/空4）       → 档位 {sc:.2f}  area {area:6.0f}  字 {fs:.1f}px")
check("全档放不下 → 取最小档并接受最小重叠", sc == 0.6, f"档位 {sc:.2f}，area {area:.0f}")

print()
print("=" * 70)
print("受压测试 2：位置搜索 —— 配置位被占时动态换位，远了要能画引线")
print("=" * 70)

reset(1504, 430)
MS._OCC.append((700.0, 150.0, 1504.0, 430.0))       # 占满配置位右侧一大片
tx, ty, ta, box, area = MS._layout(752, 215, 14, -10, "start",
                                   GC.text_w(TXT, BASE) + 26, BASE * 0.86)
gap = max(box[0] - 752, 0, 752 - box[2], box[1] - 215, 0, 215 - box[3])
print(f"  配置位整片被占 → 落点 ({tx:.0f},{ty:.0f}) 锚向 {ta}  area {area:.0f}  间隙 {gap:.0f}")
check("离开了配置位", abs(tx - 766) > 1 or abs(ty - 205) > 1, f"({tx:.0f},{ty:.0f})")
check("零重叠", area == 0)
check("外扩后间隙 > 34 → 灰线引线可画（用户明确要保留）", gap > 34, f"间隙 {gap:.0f}px")

reset(1504, 430)
MS._OCC.append((766.0, 195.0, 1000.0, 225.0))       # 只堵配置位正右方一小条
tx, ty, ta, box, area = MS._layout(752, 215, 14, -10, "start",
                                   GC.text_w(TXT, BASE) + 26, BASE * 0.86)
print(f"  仅堵配置位 → 落点 ({tx:.0f},{ty:.0f}) 锚向 {ta}  area {area:.0f}")
check("局部被占 → 就近换位（不甩远）", area == 0 and abs(ty - 205) <= 40)

print()
print("=" * 70)
print("受压测试 3：图例动态缩小")
print("=" * 70)
# 图例高度由「日程行数 + 符号类数」决定（`_legend_geom` 读 route_def）。
# 模板 route_def 只有 0 天 / 极少符号 → 基准高 ~67 px，任何预算都放得下，
# 收缩路径根本不会触发。这里 stub 出一个 **10 天 + 6 类符号**的线，
# 才是会顶到边的真实场景（北天山中线 9 天实测基准高 ≈ 340 px）。
_real_rows, _real_syms = MS._legend_rows, MS._legend_symbols
MS._legend_rows = lambda: [(f"D{i}", f"#000000", 0.0) for i in range(1, 11)]
MS._legend_symbols = lambda: [(k, k) for k in
                              ("start", "end", "camp", "water", "warn", "peak_hi")]
try:
    need = MS._legend_geom()["h"] * MS.FURN_LG
    for h in (3446, 900, 500, 300, 200):
        s = MS._legend_scale(h)
        print(f"  图高 {h:5d} → scale {s:.3f}   基准需 {need:.0f} px / 预算 {max(180.0, h * 0.42):.0f}")
    check("高图不缩", MS._legend_scale(3446) == MS.FURN_LG)
    check("矮图收缩", MS._legend_scale(300) < MS.FURN_LG)
    check("不低于下限 0.34", MS._legend_scale(150) >= 0.34, f"{MS._legend_scale(150):.3f}")
    w1, h1 = MS._legend_size(1.0)
    w2, h2 = MS._legend_size(0.5)
    check("宽高同系数（不走形）", (w2, h2) == (round(w1 * 0.5), round(h1 * 0.5)))
    check("_LG_S 同步刷新（渲染读同一个）", abs(MS._LG_S - 0.5) < 1e-9)
finally:
    MS._legend_rows, MS._legend_symbols = _real_rows, _real_syms

print()
print(("全部受压测试通过 ✓" if ok else "有未通过项 ✗"))
sys.exit(0 if ok else 1)
