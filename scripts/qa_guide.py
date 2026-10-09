# -*- coding: utf-8 -*-
"""交付前自检（通用版）：① 切边扫描 ② 版式审计 ③ 对比度 ④ 三件套是否压轨迹。

全部走"数值断言"，不产出"检查图"——看不到图的时候，肉眼检查是自欺欺人。

① 切边：正常成图最外 2 px 应是底色；边缘出现大量深色像素 = 被裁。
   注意：**满幅的图片型产物（高清地图）边缘天然可能压到深色地形**，阈值要单独放宽，
   真正的判据是 1:1 目视裁切（人手确认一次即可），别拿数字当唯一证据。
② 版式：结果写进 <body data-qa="...">，用 --dump-dom 取回（无头下拿不到 console）。
   只做 DOM 排版不做光栅化，**一次 0.5 s**，所以桌面 / 手机两个宽度各跑一次也不心疼。
③ 对比度：每个"有直接文字"的元素算实际 color 与最近不透明背景的对比度，
   <4.5:1 报警（≥18.66 px 粗体或 ≥24 px 普通字按大字号 3:1 判）。
④ 三件套压轨迹：由 map_svg.self_check 在出图时同步打印（图例/指北针/比例尺压到的轨迹点数必须为 0）。
"""
import json
import re
import subprocess
import sys

import numpy as np
from PIL import Image

import guide_common as GC
import route_def as RD

CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
BROWSER = GC.find_browser()

# (文件名, 期望宽, 边缘深色占比阈值)
IMGS = [
    (f"{STEM}-攻略长图.png", None, 0.02),
    (f"{STEM}-全线地图.jpg", None, 0.30),
]

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
  var out={vpW:de.clientWidth, docW:de.scrollWidth, pageW:+pr.width.toFixed(1),
           overflowX:de.scrollWidth-de.clientWidth, bad:[], clipped:[], tiny:[],
           low:[], lowN:0};
  var all=document.querySelectorAll('body *');
  for(var i=0;i<all.length;i++){
    var e=all[i], r=e.getBoundingClientRect();
    if(r.width===0&&r.height===0) continue;
    if(r.right>pr.right+0.6||r.left<pr.left-0.6){
      out.bad.push({tag:e.tagName,cls:(e.className||'').toString().slice(0,28),
                    l:+r.left.toFixed(1),r:+r.right.toFixed(1)});
    }
    if(e.scrollWidth>e.clientWidth+1&&e.clientWidth>0&&
       getComputedStyle(e).overflowX!=='visible'){
      out.clipped.push({tag:e.tagName,cls:(e.className||'').toString().slice(0,28),
                        sw:e.scrollWidth,cw:e.clientWidth});
    }
    var direct='';
    for(var k=0;k<e.childNodes.length;k++){
      if(e.childNodes[k].nodeType===3) direct+=(e.childNodes[k].nodeValue||'');
    }
    direct=direct.trim();
    var cs=getComputedStyle(e), fs=parseFloat(cs.fontSize), fw=parseInt(cs.fontWeight,10)||400;
    if(fs&&fs<11&&(e.textContent||'').trim().length>0) out.tiny.push(fs);
    if(direct.length>1&&cs.opacity!=='0'&&cs.visibility!=='hidden'){
      var L1=lum(cs.color), L2=lum(bgOf(e));
      if(L1!==null&&L2!==null){
        var hi=Math.max(L1,L2), lo=Math.min(L1,L2);
        var ratio=(hi+0.05)/(lo+0.05);
        var need=(fs>=24)||(fs>=18.66&&fw>=700)?3.0:4.5;
        if(ratio<need-1e-6){
          out.low.push({cls:(e.className||'').toString().slice(0,24), fs:+fs.toFixed(1),
                        fw:fw, ratio:+ratio.toFixed(2), need:need, t:direct.slice(0,14)});
        }
      }
    }
  }
  out.badN=out.bad.length; out.clippedN=out.clipped.length; out.lowN=out.low.length;
  out.bad=out.bad.slice(0,8); out.clipped=out.clipped.slice(0,8); out.low=out.low.slice(0,10);
  out.tinyMin=out.tiny.length?Math.min.apply(null,out.tiny):null;
  bd.setAttribute('data-qa',JSON.stringify(out));
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


def audit(width, label):
    src = (GC.ROOT / f"{STEM}-攻略.html").read_text(encoding="utf-8")
    tmp = GC.ROOT / f"_qa_{label}.html"
    tmp.write_text(src.replace("</body>", AUDIT_JS + "</body>", 1), encoding="utf-8")
    try:
        p = subprocess.run([BROWSER, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--no-sandbox", f"--window-size={width},2200", "--dump-dom",
                            tmp.as_uri()], capture_output=True, timeout=300)
        dom = p.stdout.decode("utf-8", "ignore")
    finally:
        tmp.unlink(missing_ok=True)
    m = re.search(r'data-qa="([^"]+)"', dom)
    if not m:
        print(f"  [{label} @{width}] 审计脚本没跑起来")
        return False
    import html as _h
    d = json.loads(_h.unescape(m.group(1)))
    ok = True
    print(f"  [{label} @{width}] 视口 {d['vpW']} · 文档宽 {d['docW']} · 版心宽 {d['pageW']}"
          f" · 横向溢出 {d['overflowX']} px")
    if d["overflowX"] > 1:
        print(f"     !! 横向溢出 {d['overflowX']} px")
        ok = False
    if d["badN"]:
        print(f"     !! {d['badN']} 个元素越出版心: {d['bad']}")
        ok = False
    if d["clippedN"]:
        print(f"     !! {d['clippedN']} 个元素内容被裁: {d['clipped']}")
        ok = False
    if d["lowN"]:
        print(f"     !! {d['lowN']} 处文字对比度不足:")
        for x in d["low"]:
            print(f"        {x['cls']:22s} {x['fs']:5.1f}px/{x['fw']:4d} "
                  f"对比 {x['ratio']:.2f} < {x['need']} · 「{x['t']}」")
        ok = False
    if d["tinyMin"]:
        print(f"     · 最小字号 {d['tinyMin']} px（<11 需确认可读性）")
    return ok


if __name__ == "__main__":
    print("① 切边扫描（最外 2 px 深色占比）")
    allok = True
    for name, w, thr in IMGS:
        p = GC.ROOT / name
        if not p.exists():
            print(f"  [..]  缺 {name}（跳过）")
            continue
        allok &= edge_scan(p, w, thr)
    print("② 版式审计 + 对比度（DOM 排版，无光栅化）")
    allok &= audit(1200, "desk")
    allok &= audit(600, "phone")
    print("\n结论：" + ("全部通过 ✓" if allok else "有问题，见上 ↑"))
    sys.exit(0 if allok else 1)
