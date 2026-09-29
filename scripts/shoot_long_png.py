# -*- coding: utf-8 -*-
"""用本机 Chrome 无头模式把攻略 HTML 渲染成分享用长图

同一份 HTML 出两个尺寸：
  · 宽屏版 —— 走桌面 CSS（1120 版心、地图＋右栏两列），保留版心外的「纸面」外框；
  · 手机版 —— 走 @media(max-width:900px) 单栏 CSS，**裁到版心、全出血**。

为什么必须有手机版：长图在手机上按满宽显示时缩放比只有 390/2344 ≈ 0.17，
宽屏版 12.8 px 的正文落到屏幕上不足 2.2 px，整块右栏等于不可读的灰噪。
430 CSS × 2 = 860 px 成品，满宽下正文约 13 个屏幕像素，直接可读。

⚠ 坑（真实返工）：**不要用 --window-size 反推裁剪区间**。
  Windows 无头 Chrome 对窗口宽有**最小钳制** —— `--window-size=430` 实测拿到的是
  `clientWidth = 500`。于是画布只有 430 CSS、版面却按 500 排：`.page`（max-width 430，
  margin auto 居中）落在 x = 35…465，截图把右侧 35 px 整条切掉，
  表现为**每一行文字末尾都缺字**（且因为 body 底色与版心同色，肉眼看不出是"被裁"）。
  → 改为**按像素量版心边界**：渲染时给 <body> 临时注入一个与版心不同的底色，
     量出版心的左右/上下边再裁；注入色永远不会出现在成品里。
  → 并断言量到的宽度 == 设计宽 × 倍率，量错立刻报警而不是静默交付。
"""
import subprocess, sys, time
import numpy as np
from pathlib import Path
from PIL import Image

ROOT = Path(r"C:/Users/S6576/WorkBuddy/2026-09-29-09-25-03/党岭拉东线攻略")
HTML = ROOT / "党岭拉东线-攻略.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SCALE = 2

# 探测版心用的临时底色（浅暖灰），与版心 #FBF8F4 明显可分；只用于量边界，不进成品
PROBE_BG = "#DED8CE"

MODES = {
    #        窗宽   窗高    输出文件名                              设计宽  裁到版心
    "desk":  (1172, 4800, "党岭拉东线-攻略长图.png",        1120,  False),
    "phone": (600,  7600, "党岭拉东线-攻略长图-手机版.png",  430,  True),
}


def _probe_html(dst):
    """写一份临时 HTML：<body> 带上探针底色，供量版心边界。"""
    src = HTML.read_text(encoding="utf-8")
    out = src.replace("<body>", f'<body style="background:{PROBE_BG}">', 1)
    assert out != src, "没找到 <body>，无法注入探针底色"
    dst.write_text(out, encoding="utf-8")


def _page_box(im):
    """量出版心矩形：整条落在版心内的列/行，其像素必然大量偏离探针底色。"""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    bg = a[2, 2]                                    # 左上角＝探针底色
    off = np.abs(a - bg).sum(axis=2) > 14

    cols = np.where(off.sum(axis=0) > 0.5 * h)[0]
    rows = np.where(off.sum(axis=1) > 0.5 * w)[0]
    assert cols.size and rows.size, "没量到版心，探针底色可能被覆盖"
    return int(cols[0]), int(cols[-1]) + 1, int(rows[0]), int(rows[-1]) + 1


def render(mode):
    win_w, win_h, name, design_w, tight = MODES[mode]
    raw = ROOT / f"_raw_{mode}.png"
    tmp = ROOT / f"_probe_{mode}.html"
    raw.unlink(missing_ok=True)
    try:
        _probe_html(tmp)
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--no-sandbox", f"--force-device-scale-factor={SCALE}",
                        "--window-size=%d,%d" % (win_w, win_h),
                        f"--screenshot={raw}", tmp.as_uri()],
                       check=True, capture_output=True, timeout=600)
    finally:
        tmp.unlink(missing_ok=True)
    time.sleep(0.5)

    im = Image.open(raw).convert("RGB")
    w, h = im.size
    x0, x1, y0, y1 = _page_box(im)

    want = round(design_w * SCALE)
    got = x1 - x0
    if abs(got - want) > 2:
        print(f"!! {mode}: 量到版心宽 {got} px，应为 {want} px（设计宽 {design_w}×{SCALE}）"
              f" —— 视口可能小于设计宽，请调大 MODES['{mode}'][0]")
    if y1 >= h - 2:
        print(f"!! {mode}: 内容顶到窗口底，请调大 MODES['{mode}'][1]")

    if tight:                                        # 手机版：裁到版心，全出血
        box = (x0, y0, x1, y1)
    else:                                            # 宽屏版：保留版心外的纸面外框
        box = (0, 0, w, min(h, y1 + 48))
    out = ROOT / name
    im.crop(box).save(out, optimize=True)
    raw.unlink(missing_ok=True)
    print(f"[{mode}] canvas {(w, h)} · page x {x0}-{x1} y {y0}-{y1} "
          f"-> {Image.open(out).size} · {out.stat().st_size/1048576:.2f} MB · {out.name}")


if __name__ == "__main__":
    for m in (sys.argv[1:] or list(MODES)):
        render(m)
