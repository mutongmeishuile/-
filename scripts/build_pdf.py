# -*- coding: utf-8 -*-
"""把成品地图 JPG 打成单页 PDF —— 直接内嵌原始 JPEG 字节，**不重新编码**。

为什么手写 PDF 而不是 PIL / reportlab
------------------------------------
· `PIL.Image.save(..., "PDF")` 会把整图**重压成 quality≈75 的 JPEG**，几十 MB 的
  成品再压一道肉眼可见地糊；要无损只能存 Flate，文件直接翻 3 倍。
· PDF 的 `/DCTDecode` 允许把 JPEG 压缩流**原样嵌进去**，解码交给阅读器 ——
  既不损画质，也不重算上亿像素。
· 环境里通常没装 reportlab / pypdf / fitz，手写一个最小 PDF（xref 偏移自检）最省事。

版面：默认按 **350 dpi** 反算页宽（`--dpi` 可改），保证不同画幅的几张总览图
      落地精度一致、物理尺寸各随其宽高比；也可以 `--width-mm` 直接指定页宽。

用法
----
    python scripts/build_pdf.py                      # 自动找本项目成品 jpg
    python scripts/build_pdf.py --dpi 300
    python scripts/build_pdf.py --width-mm 600
    python scripts/build_pdf.py ../某图.jpg --dpi 350   # 指定文件
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

MM2PT = 72.0 / 25.4          # 1 mm = 2.8346 pt


def build(jpg: Path, pdf: Path, page_w_mm: float):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(jpg) as im:
        w_px, h_px = im.size
    page_w = page_w_mm * MM2PT
    page_h = page_w * h_px / w_px

    data = jpg.read_bytes()
    content = f"q {page_w:.3f} 0 0 {page_h:.3f} 0 0 cm /Im0 Do Q\n".encode("latin-1")

    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append((f"<< /Type /Page /Parent 2 0 R "
                 f"/MediaBox [0 0 {page_w:.3f} {page_h:.3f}] "
                 f"/Resources << /XObject << /Im0 5 0 R >> >> "
                 f"/Contents 4 0 R >>").encode("latin-1"))
    objs.append((b"<< /Length %d >>\nstream\n" % len(content)) + content + b"endstream")
    objs.append((f"<< /Type /XObject /Subtype /Image /Width {w_px} /Height {h_px} "
                 f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                 f"/Length {len(data)} >>\nstream\n").encode("latin-1")
                + data + b"\nendstream")
    # 标题含中文 → 必须 UTF-16BE + BOM 的十六进制串，latin-1 装不下
    title_hex = (b"\xfe\xff" + pdf.stem.encode("utf-16-be")).hex().upper()
    objs.append((f"<< /Title <{title_hex}> /Creator (hiking-route-guide-poster) "
                 f"/Producer (build_pdf.py) >>").encode("latin-1"))

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, body in enumerate(objs, start=1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += (b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, len(objs), xref))
    pdf.write_bytes(bytes(out))

    raw = pdf.read_bytes()
    for i, o in enumerate(offs, start=1):
        assert raw[o:o + len(b"%d 0 obj" % i)] == b"%d 0 obj" % i, f"对象 {i} 偏移错"
    print(f"  PDF {pdf.name}  {pdf.stat().st_size / 1048576:.1f} MB  "
          f"{page_w_mm:.0f} × {page_w_mm * h_px / w_px:.0f} mm  "
          f"（{w_px}×{h_px} px，落地 {w_px / (page_w_mm / 25.4):.0f} dpi）")


def find_jpg():
    """优先 route_def 的 file_stem；本目录下最大（= 成品高清图）的那张兜底。"""
    try:
        import route_def as RD
        p = ROOT / f'{RD.CFG["file_stem"]}.jpg'
        if p.exists():
            return p
    except Exception:                                              # noqa
        pass
    cands = [p for p in ROOT.glob("*.jpg") if "长图" not in p.name]
    if not cands:
        raise SystemExit(f"{ROOT} 下没有可用 jpg")
    return max(cands, key=lambda p: p.stat().st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jpg", nargs="?", default=None)
    ap.add_argument("--dpi", type=float, default=350.0,
                    help="落地精度（默认 350），页宽 = 像素宽 / dpi × 25.4")
    ap.add_argument("--width-mm", type=float, default=None, help="直接指定页宽（mm），压过 --dpi")
    a = ap.parse_args()
    jpg = Path(a.jpg) if a.jpg else find_jpg()
    if not jpg.is_absolute():
        jpg = (Path.cwd() / jpg).resolve()
    w_px = None
    if a.width_mm:
        w_mm = a.width_mm
    else:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(jpg) as im:
            w_px = im.size[0]
        w_mm = w_px / a.dpi * 25.4
    build(jpg, jpg.with_suffix(".pdf"), w_mm)


if __name__ == "__main__":
    main()
