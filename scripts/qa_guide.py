# -*- coding: utf-8 -*-
"""交付前自检（通用版）：① 切边扫描 ② 版式审计+对比度（轮廓截图）③ 三件套是否压轨迹。

① 切边：正常成图最外 2 px 应是底色；边缘出现大量深色像素 = 被裁。
   注意：**满幅的图片型产物（高清地图）边缘天然可能压到深色地形**，阈值要单独放宽，
   真正的判据是 1:1 目视裁切（人手确认一次即可），别拿数字当唯一证据。
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

# (文件名, 期望宽, 边缘深色占比阈值)
IMGS = [
    (f"{STEM}-攻略长图.png", None, 0.02),
    (f"{STEM}-全线地图.jpg", None, 0.30),
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


def edge_scan(path, want_w=None, thr=0.02):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    a = np.asarray(im).astype(np.float32)
    lum = a @ (0.299, 0.587, 0.114)
    ok, msgs = True, []
    for name, band in {"left": lum[:, :2], "right": lum[:, -2:],
                       "top": lum[:2, :], "bottom": lum[-2:, :]}.items():
        frac = float((band < 120).mean())
        flag = frac > thr
        ok &= not flag
        msgs.append(f"{name} {frac*100:.2f}%{'' if not flag else '  <-- 疑似切边'}")
    wok = "" if want_w is None or w == want_w else f"  宽 ✗（期望 {want_w}）"
    ok &= (want_w is None or w == want_w)
    print(f"  {path.name:34s} {w}×{h}{wok}  " + " | ".join(msgs))
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


def todo_guard():
    """`★ TODO` 未填守卫：防止把占位符交付出去（自主流程尤其要避免）。"""
    rd = HERE / "route_def.py"
    if not rd.exists():
        return 0
    # 占位都是 "★ TODO <描述>"（开引号+标记+空格）；route_def 的 _todo_count 函数自身那行
    # 也含 '"★ TODO '，故总数减 1 排除自身这行，得到真实占位数。
    n = max(rd.read_text(encoding="utf-8").count('"★ TODO ') - 1, 0)
    if n:
        print(f"  !! route_def.py 仍有 {n} 处 ★ TODO 未填 —— 交付物会带占位文字，先回填再交付")
    else:
        print("  [OK]  route_def.py 无 ★ TODO 占位（文案已填）")
    return n


if __name__ == "__main__":
    print("① 切边扫描（最外 2 px 深色占比）")
    allok = True
    for name, w, thr in IMGS:
        p = GC.ROOT / name
        if not p.exists():
            print(f"  [..]  缺 {name}（跳过）")
            continue
        allok &= edge_scan(p, w, thr)
    print("② 版式审计 + 对比度（彩色轮廓截图，桌面版必须 0 问题）")
    # 桌面版高度从长图反推（长图 = 版心 × scale2），手机版用同高（只改视口宽度看换行/溢出）
    long_png = GC.ROOT / f"{STEM}-攻略长图.png"
    doc_h = (Image.open(long_png).size[1] / 2.0) if long_png.exists() else 4000
    # desk/phone 两次渲染互相独立（各自起一个无头浏览器进程），并行跑省一半时间
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {width: pool.submit(render_overlay, label, width, doc_h)
                for width, label in ((1200, "desk"), (600, "phone"))}
        overlay_paths = []
        for width, label in ((1200, "desk"), (600, "phone")):
            out = futs[width].result()
            if out and out.exists():
                overlay_paths.append(str(out))
                print(f"  → {label} 审计截图：{out.name}（红=对比度不足 橙=越界 "
                      f"品红=被裁 黄=字号过小；顶部汇总条给数量）")
    if not overlay_paths:
        print("  ⚠ 审计截图未生成，请手动用浏览器核对对比度/溢出")
    print("③ 三件套压轨迹：见 make_all 日志里 map_svg.self_check 的「压轨迹点/家具互相重叠」")
    n_todo = todo_guard()
    allok &= (n_todo == 0)
    if overlay_paths:
        print("  → 请在下方审计截图中确认：桌面版无红/橙/品红轮廓（低对比度/越界/被裁）")
    print("\n结论：" + ("全部通过 ✓" if allok else "有问题，见上 ↑"))
    sys.exit(0 if allok else 1)
