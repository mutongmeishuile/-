# -*- coding: utf-8 -*-
"""重建地图署名徽标：`assets/src/avatar_C_contour_ring.svg` → `assets/avatar_ring.svg`。

什么时候需要跑它：
  · 换头像照片 / 改昵称或小红书号 / 想把徽标里那两行字再放大一点。
  · 平时不用跑 —— `assets/avatar_ring.svg` 已经是构建好的成品（116 KB，自包含）。

它做的事（也是当初为什么要有这一步）：
  1. 原稿 881 KB，其中 854 KB 是 1200×1200 的 PNG 照片 → 压到 640×640 JPEG（约 108 KB）；
  2. **刻度环和两行文字保持矢量** —— 地图上放大看不会糊，这是清晰度的根；
  3. 文字重排：原稿账号字号只有 22/1200，缩到地图徽标（直径 120–140 逻辑 px）上
     只剩 3.7 px 字高，矢量也读不出；改成 名称 66 / 账号 46、两行拉开，
     1:1 下账号约 14.7 px，放大 2 倍仍能读全。

用法：
    python scripts/build_badge_svg.py                      # 用默认值重建
    python scripts/build_badge_svg.py --account 559314345  # 换号（会写进两处：描边层 + 前景层）
    python scripts/build_badge_svg.py --name-size 70 --id-size 50 --photo 800
"""
import argparse
import base64
import io
import re
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
SRC = ASSETS / "src" / "avatar_C_contour_ring.svg"
OUT = ASSETS / "avatar_ring.svg"

DEFAULT_ACCOUNT = "559314345"        # 9 位，2026-10-26 用户本人确认（别再"补"成 11 位）
BAD_ACCOUNT = "55931434345"          # 曾经误改出来的 11 位，出现即纠正


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--photo", type=int, default=640, help="内嵌照片边长（越大越清晰、体积越大）")
    ap.add_argument("--name-size", type=int, default=66, help="「鹏鹏」字号（原稿 78）")
    ap.add_argument("--id-size", type=int, default=46, help="账号行字号（原稿 22）")
    a = ap.parse_args()

    if not SRC.exists():
        sys.exit(f"找不到源文件：{SRC}")
    s = SRC.read_text(encoding="utf-8")

    # ---- 1. 照片降采样 ----
    m = re.search(r'href="data:image/png;base64,([^"]+)"', s)
    if not m:
        sys.exit("源文件里找不到内嵌照片（data:image/png;base64）")
    raw = base64.b64decode(m.group(1))
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(im.size) > a.photo:
        im = im.resize((a.photo, a.photo), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True, progressive=True)
    s = s[:m.start(1)] + base64.b64encode(buf.getvalue()).decode("ascii") + s[m.end(1):]
    s = s.replace("data:image/png;base64,", "data:image/jpeg;base64,", 1)

    # ---- 2. 账号（含纠错 + 断言）----
    fixed = s.count(BAD_ACCOUNT)
    if fixed:
        s = s.replace(BAD_ACCOUNT, a.account)
    for old in set(re.findall(r"小红书 · (\d+)", s)):
        if old != a.account:
            s = s.replace(f"小红书 · {old}", f"小红书 · {a.account}")
    n = s.count(f"小红书 · {a.account}")
    if n != 2:
        sys.exit(f"账号出现 {n} 次，应为 2（描边层 + 前景层）—— 源文件结构变了，先看一眼")

    # ---- 3. 文字排版（原稿 78 / 22 → 默认 66 / 46，并抬高基线给账号腾地方）----
    s = s.replace('font-size="78" font-weight="700" letter-spacing="10"',
                  f'font-size="{a.name_size}" font-weight="700" letter-spacing="8"')
    s = s.replace('font-size="22" letter-spacing="3"',
                  f'font-size="{a.id_size}" letter-spacing="1.5"')

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(s, encoding="utf-8")
    print(f"  → {OUT}  {OUT.stat().st_size // 1024} KB")
    print(f"     账号 {a.account} · 字号 {a.name_size}/{a.id_size} · 照片 {min(im.size)} px")
    if fixed:
        print(f"     （纠正了 {fixed} 处旧的 11 位错值）")


if __name__ == "__main__":
    main()
