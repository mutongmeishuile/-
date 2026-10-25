# -*- coding: utf-8 -*-
"""交付前自检（通用版）：① 切边扫描 ② 版式审计+对比度（轮廓截图）③ 三件套是否压轨迹。

① 切边：正常成图最外 2 px 应是底色；边缘出现大量深色像素 = 被裁。
   注意：**满幅的图片型产物（高清地图）边缘天然可能压到深色地形**，阈值要单独放宽。
   对满幅底图（`IMGS` 里 `full_bleed=True`）真正的判据是 **`track_inside()`：轨迹是否
   完整落在画布内** —— 深色占比只作粗略提示，超了但轨迹在内就不算问题
   （北天山 3800 m 雪线 + 深绿林区线路实测 top 冲到 44%，轨迹四边余量 104–3682 px）。
   ⚠ 别用"继续抬阈值"来消警：那是把真裁切一起盖掉。有留白的产物（长图）仍用 0.02。
② 版式审计 + 对比度：本机沙箱禁回环监听（127.0.0.1 连不通），`--dump-dom` 也无输出，
   所以**不再用 DOM 取回数据**，改成「把问题元素用彩色轮廓画进页面 + 顶部固定汇总条，再截图」。
   多模态模型直接看截图即可定位（红=对比度不足 橙=越界 品红=被裁 黄=字号过小）。
   桌面版是出长图那版、必须 0 问题；手机版拥挤只告警。
③ 三件套压轨迹：由 map_svg.self_check 在出图时同步打印（图例/指北针/比例尺压到的轨迹点数必须为 0）。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

import guide_common as GC
import route_def as RD

HERE = Path(__file__).resolve().parent
CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
BROWSER = GC.find_browser()

# (文件名, 期望宽, 边缘深色占比阈值, 是否满幅底图)
# ⚠ 全线地图 0.30 → **0.40**（2026-10-17 希夏邦马）：这条是**满幅地形图**（底图铺满画幅、
#   没有留白边框），边缘 2 px 天然压在地形上；判据 `亮度 < 120` 会把背光坡、深谷、
#   等高线数字一起算成"深色"。实测把冰川层（近白填充 alpha 150，等于给雪区提亮）关掉后，
#   bottom 由 22.6% 升到 33.0%、left 由 21.4% 升到 28.0% —— **不是内容被裁**
#   （尺寸仍为底图 3008×4904、轨迹未溢出），而是那层提亮没了、地形暗部露出来。
#   → 对满幅底图类产物，切边只能靠**尺寸与轨迹完整性**判断，深色占比只作粗略提示。
# ⚠⚠ 2026-10-17 北天山中线：最低档「山地森林深绿」(104,124,96)（亮度 ≈115 < 120）
#   叠加晕渲后整片露在边缘，top 冲到 **44%** 越过 0.40 —— 同样不是裁切（轨迹四边余量
#   104–3682 px）。0.40 这个数对深绿林区线路本来就不够，**别再往上抬阈值**
#   （那是把真问题盖掉），改成 `full_bleed=True` 走「轨迹完整性」这条真判据。
IMGS = [
    (f"{STEM}-攻略长图.png", None, 0.02, False),
    (f"{STEM}-全线地图.jpg", None, 0.40, True),
]

# QA 审计的两种取回方式都不依赖回环监听（本机沙箱禁 127.0.0.1，信标/CDP 都走不通）：
#   · 直接把"问题元素"用彩色轮廓画出来 + 顶部固定一条**人/模型可读的汇总条**，
#     再截图。模型（多模态）直接看截图就能定位问题，无需 DOM 取回。
#   · 颜色：红=对比度不足(<4.5:1) 橙=越出版心 品红=内容被裁 黄=字号<11px。
AUDIT_JS = r"""
<script>
(function(){
  function lum(c){
    var m = c.match(/[\d.]+/g); if(!m) return null;
    var a = m.slice(0,3).map(function(v){v=+v/255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);});
    return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];
  }
  function bgOf(e){
    var n=e;
    while(n && n.nodeType===1){
      var m = getComputedStyle(n).backgroundColor.match(/[\d.]+/g);
      if(m && (m.length<4 || +m[3]>0.85) && !(m[0]==='0'&&m[1]==='0'&&m[2]==='0')) return getComputedStyle(n).backgroundColor;
      n=n.parentElement;
    }
    return 'rgb(255,255,255)';
  }
  var de=document.documentElement, bd=document.body;
  var page=document.querySelector('.page')||bd;
  var pr=page.getBoundingClientRect();
  var bad=[], clipped=[], low=[], tiny=[], n_low=0;
  var all=document.querySelectorAll('body *');
  for(var i=0;i<all.length;i++){
    var e=all[i], r=e.getBoundingClientRect();
    if(r.width===0&&r.height===0) continue;
    if(r.right>pr.right+0.6||r.left<pr.left-0.6) bad.push(e);
    if(e.scrollWidth>e.clientWidth+1&&e.clientWidth>0&&getComputedStyle(e).overflowX!=='visible') clipped.push(e);
    var direct='';
    for(var k=0;k<e.childNodes.length;k++){
      if(e.childNodes[k].nodeType===3) direct+=(e.childNodes[k].nodeValue||'');
    }
    direct=direct.trim();
    var cs=getComputedStyle(e), fs=parseFloat(cs.fontSize), fw=parseInt(cs.fontWeight,10)||400;
    if(fs&&fs<11&&(e.textContent||'').trim().length>0) tiny.push(fs);
    if(direct.length>1&&cs.opacity!=='0'&&cs.visibility!=='hidden'){
      var L1=lum(cs.color), L2=lum(bgOf(e));
      if(L1!==null&&L2!==null){
        var hi=Math.max(L1,L2), lo=Math.min(L1,L2), ratio=(hi+0.05)/(lo+0.05);
        var need=(fs>=24)||(fs>=18.66&&fw>=700)?3.0:4.5;
        if(ratio<need-1e-6){ low.push(e); n_low++; }
      }
    }
  }
  var ovx=de.scrollWidth-de.clientWidth;
  bad.forEach(function(e){ e.style.outline='4px solid #ff7a00'; e.style.backgroundColor='rgba(255,122,0,.35)'; });
  clipped.forEach(function(e){ e.style.outline='4px solid #ff00ff'; e.style.backgroundColor='rgba(255,0,255,.30)'; });
  low.forEach(function(e){ e.style.outline='4px solid #ff2d2d'; e.style.backgroundColor='rgba(255,45,45,.32)'; });
  tiny.forEach(function(e){ e.style.outline='4px solid #d4b000'; e.style.backgroundColor='rgba(255,230,0,.35)'; });
  var d=document.createElement('div');
  d.style.cssText='position:fixed;top:0;left:0;right:0;z-index:99999;background:#000;'
    +'color:#fff;font:40px/1.35 "Microsoft YaHei",sans-serif;font-weight:700;'
    +'padding:14px 18px;white-space:pre-wrap;border-bottom:6px solid #ff2d2d';
  d.textContent='QA 自检\n横向溢出 '+ovx+'px ｜ 越界 '+bad.length
    +' ｜ 被裁 '+clipped.length+' ｜ 低对比度 '+low.length+' ｜ 字号<11px '+tiny.length
    +'\n(红=低对比度 橙=越界 品红=被裁 黄=字号小)';
  bd.appendChild(d);
  bd.style.paddingTop='200px';
})();
</script>
"""


def track_inside(img_path):
    """满幅底图的**真裁切判据**：轨迹是否完整落在画布内（带余量）。

    为什么需要它：满幅地形图铺满画幅、没有留白边框，`edge_scan` 的「亮度 < 120」
    会把背光坡、深谷、等高线数字一起算成深色 —— 北天山实测把最低档「山地森林深绿」
    (104,124,96)（亮度 ≈115）叠加晕渲后露在边缘，top 冲到 44%，**但内容一点没裁**
    （轨迹四边余量 104–3682 px）。深色占比对这类产物只是粗略提示，
    真正的判据是尺寸 + 轨迹完整性（见下面 IMGS 的注释）。
    返回 (ok, 说明)；拿不到 track_full.json / base_meta.json 时返回 (None, 原因)。
    """
    tf, meta_p = HERE / "out" / "track_full.json", HERE / "out" / "base_meta.json"
    if not (tf.exists() and meta_p.exists()):
        return None, "缺 out/track_full.json 或 base_meta.json，跳过"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        pts = json.loads(tf.read_text(encoding="utf-8"))["pts"]
        P = GC.projector_of(meta)
        iw, ih = Image.open(img_path).size
        xy = np.array([P(p[0], p[1]) for p in pts], dtype=float)
        m = [xy[:, 0].min(), iw - xy[:, 0].max(), xy[:, 1].min(), ih - xy[:, 1].max()]
        names = ["左", "右", "上", "下"]
        ok = all(v >= 0 for v in m)
        return ok, "四边余量 " + " / ".join(f"{n} {v:.0f} px" for n, v in zip(names, m))
    except Exception as e:                                     # noqa: BLE001
        return None, f"投影核对失败：{e}"


def edge_scan(path, want_w=None, thr=0.02, full_bleed=False):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    a = np.asarray(im).astype(np.float32)
    lum = a @ (0.299, 0.587, 0.114)
    ok, msgs = True, []
    tripped = []
    for name, band in {"left": lum[:, :2], "right": lum[:, -2:],
                       "top": lum[:2, :], "bottom": lum[-2:, :]}.items():
        frac = float((band < 120).mean())
        flag = frac > thr
        if flag:
            tripped.append(name)
        msgs.append(f"{name} {frac*100:.2f}%{'' if not flag else '  <-- 超阈值'}")
    wok = "" if want_w is None or w == want_w else f"  宽 ✗（期望 {want_w}）"
    ok &= (want_w is None or w == want_w)
    print(f"  {path.name:34s} {w}×{h}{wok}  " + " | ".join(msgs))
    if not tripped:
        return ok
    # 超阈值了。满幅底图：判据是轨迹完整性，深色占比只作提示（见 IMGS 注释）。
    if full_bleed:
        tok, why = track_inside(path)
        if tok is True:
            print(f"     · 满幅底图：{why} → **轨迹完整在内，不是裁切**；"
                  f"深色来自地形暗部（森林深绿叠加晕渲），按既定口径只作提示")
            return ok                      # 不计入失败
        if tok is False:
            print(f"     · ✗ 轨迹不在画布内（{why}）→ 这是真裁切，窗口/投影要查")
            return False
        print(f"     · ⚠ {why} → 无法自动判定，按保守口径仍计为异常，请 1:1 目视确认无裁切")
        return False
    return ok


def render_overlay(label, width, doc_h):
    """把审计 JS 注入攻略 HTML，渲染一张「问题元素彩色轮廓 + 顶部汇总条」的截图。

    不用回环监听 / 不用 --dump-dom：直接截图，多模态模型看截图即可定位问题。
    返回截图路径；若长图不存在（拿不到整页高度）则跳过并返回 None。
    """
    long_png = GC.ROOT / f"{STEM}-攻略长图.png"
    if not long_png.exists():
        print(f"  ⚠ [{label} @{width}] 缺长图，无法定位整页高度，跳过该宽度审计")
        return None
    src = (GC.ROOT / f"{STEM}-攻略.html").read_text(encoding="utf-8")
    tmp = GC.ROOT / f"_qa_{label}.html"
    tmp.write_text(src.replace("</body>", AUDIT_JS + "</body>", 1), encoding="utf-8")
    out = GC.ROOT / f"_qa_{label}.png"
    try:
        GC.browser_shot(tmp, out, width, int(doc_h) + 60, scale=1)
    finally:
        tmp.unlink(missing_ok=True)
    return out


MARK = "★ TODO"          # 待办标记（与 new_route.TODO 同源，改一处要同步另一处）


def todo_guard():
    """`★ TODO` 未填守卫：防止把占位符交付出去（自主流程尤其要避免）。

    2026-10-22 修：判据从 `'"★ TODO '`（前引号紧贴标记）放宽为**数全部 `'★ TODO'`**。
    旧判据只是为了省掉「排除 _todo_count 自身那行」这一步，代价是把**不以引号开头**
    的占位整个漏掉 —— `CFG["foot"]` 的 `<b>安全提示：</b>★ TODO …` 与 notes 里的
    `</b>，★ TODO 折算成…` 正是这个形态，结果 QA 报「无占位 ✓」而页面照印占位文字，
    漏掉的偏偏是最要紧的安全提示。

    放宽的前提：new_route 生成的骨架已把标记拆成 `"★" + " TODO"` 两段拼接，
    故 _todo_count() 自身不再命中计数，可以放心全量统计。
    """
    rd = HERE / "route_def.py"
    if not rd.exists():
        return 0
    txt = rd.read_text(encoding="utf-8")
    n = txt.count(MARK)
    # 改版前的旧骨架：_todo_count() 的函数体与注释里各含一次完整标记 → 虚增 2
    if "_todo_count" in txt and ('"%s ' % MARK) in txt:
        print("  [..]  旧骨架（改版前生成）：_todo_count() 自身含标记字面量，"
              "下面这个数会虚增 2 —— 真值自己减掉，或重新跑一次 new_route.py")
    if n:
        print(f"  !! route_def.py 仍有 {n} 处 {MARK} 未填 —— 交付物会带占位文字，先回填再交付")
    else:
        print(f"  [OK]  route_def.py 无 {MARK} 占位（文案已填）")
    return n


def snowline_legend_guard():
    """图例雪线 desc 与出图雪线一致性守卫（2026-10-25 增）。

    渲染期 build_guide / map_svg 已经各检一次（GC.check_snowline_legend），
    这里再独立检一次：qa_guide 常被单独跑（`make_all` 里也是独立步骤），
    不能假设同一次运行里渲染步骤恰好也跑过。
    """
    try:
        import route_def as RD
        cfg = getattr(RD, "CFG", {}) or {}
        m = GC.load_meta() or {}
        s = m.get("snowline")
        if s is None:
            return
        val = int(round(float(s)))
        bad = []
        for it in cfg.get("legend_extra") or []:
            if "雪线" not in str(it.get("tag", "")):
                continue
            nums = {int(x) for x in re.findall(r"\d{3,5}", str(it.get("desc", "")))}
            if val not in nums:
                bad.append(str(it.get("desc", "")))
        if bad:
            print(f"  !! 图例雪线 desc 与出图值 {val} m 不符：{bad}")
            print("     → 改 route_def.CFG['legend_extra'] 的 desc（出图值 = ELA ± 月档偏移）")
        else:
            print(f"  [OK]  图例雪线 desc 与出图值 {val} m 一致")
    except SystemExit:
        raise
    except Exception as _e:                                        # noqa
        print(f"  [..]  雪线图例检查跳过：{_e}")


if __name__ == "__main__":
    # 本技能**只出宽屏长图**（手机版长图已废弃，手机阅读直接看 HTML）。
    # 所以 600 px 那趟只是"看手机换行有没有崩"的探针，不是产物 —— 不想看就 --no-phone，
    # 省一趟无头浏览器（也顺带绕开下面那个"两个实例互相抢资源"的老毛病）。
    import argparse
    ap = argparse.ArgumentParser(description="攻略交付前自检")
    ap.add_argument("--no-phone", action="store_true",
                    help="只审桌面版（技能不出手机版长图时用）")
    ap.add_argument("--no-shots", action="store_true",
                    help="完全跳过审计截图（只看切边扫描与占位符守卫）")
    args = ap.parse_args()

    print("① 切边扫描（最外 2 px 深色占比）")
    allok = True
    for name, w, thr, fb in IMGS:
        p = GC.ROOT / name
        if not p.exists():
            print(f"  [..]  缺 {name}（跳过）")
            continue
        allok &= edge_scan(p, w, thr, full_bleed=fb)
    print("② 版式审计 + 对比度（彩色轮廓截图，桌面版必须 0 问题）")
    # 桌面版高度从长图反推（长图 = 版心 × scale2），手机版用同高（只改视口宽度看换行/溢出）
    long_png = GC.ROOT / f"{STEM}-攻略长图.png"
    doc_h = (Image.open(long_png).size[1] / 2.0) if long_png.exists() else 4000
    # desk/phone 两次渲染互相独立（各自起一个无头浏览器进程），并行跑省一半时间。
    # ⚠ 2026-10-23 大横断海子线实测：**两个无头浏览器同时起会互相打架** —— 总有一个
    # 以 rc=21 退出（两张图交替失败，每次不同的那张），串行跑则都成功。
    # 所以这里并行失败后自动退回串行重试，别把"环境抢资源"误报成"审计截图生成不了"。
    from concurrent.futures import ThreadPoolExecutor
    overlay_paths = []
    if args.no_shots:
        _jobs = ()
        print("  [..] --no-shots：跳过审计截图")
    else:
        _jobs = ((1200, "desk"), (600, "phone")) if not args.no_phone else ((1200, "desk"),)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {width: pool.submit(render_overlay, label, width, doc_h)
                for width, label in _jobs}
        _failed = []
        for width, label in _jobs:
            try:
                out = futs[width].result()
            except Exception as e:                       # noqa
                print(f"  [..] {label} 并行渲染失败（{type(e).__name__}），转串行重试")
                _failed.append((width, label))
                continue
            if out and out.exists():
                overlay_paths.append(str(out))
                print(f"  → {label} 审计截图：{out.name}（红=对比度不足 橙=越界 "
                      f"品红=被裁 黄=字号过小；顶部汇总条给数量）")
    for width, label in _failed:
        try:
            out = render_overlay(label, width, doc_h)
        except Exception as e:                           # noqa
            print(f"  [!!] {label} 串行也失败：{e}")
            continue
        if out and out.exists():
            overlay_paths.append(str(out))
            print(f"  → {label} 审计截图（串行）：{out.name}")
    if not overlay_paths and not args.no_shots:
        print("  ⚠ 审计截图未生成，请手动用浏览器核对对比度/溢出")
    print("③ 三件套压轨迹：见 make_all 日志里 map_svg.self_check 的「压轨迹点/家具互相重叠」")
    n_todo = todo_guard()
    allok &= (n_todo == 0)
    snowline_legend_guard()
    if overlay_paths:
        print("  → 请在下方审计截图中确认：桌面版无红/橙/品红轮廓（低对比度/越界/被裁）")
    print("\n结论：" + ("全部通过 ✓" if allok else "有问题，见上 ↑"))
    sys.exit(0 if allok else 1)
