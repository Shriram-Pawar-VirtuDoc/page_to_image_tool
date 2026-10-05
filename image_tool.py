#!/usr/bin/env python3
# requirements: pip install -r requirements.txt   (pypdfium2, Pillow)
"""
Image Tool - render one PDF page to a compact PNG.

Run:  python image_tool.py
Then enter the PDF path, the page number (PDF page position, 1 = first page)
and choose:
  1) Dynamic DPI - DPI picked from the page's own picture resolution;
     output capped at 3200 px long edge and fitted to ~1.5 MB.
  2) Select DPI  - render at exactly 300 / 450 / 600 / 900 / 1200 DPI.

PNGs are kept small with an adaptive 256-colour palette + dithering
(visually near-identical to full colour, ~60% smaller). Pages with few
colours (charts, text) and grayscale pages are saved losslessly.
"""

from __future__ import annotations

import math
import os
import re
import sys
import tempfile
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
except ImportError:
    sys.exit("ERROR: pypdfium2 is not installed. Run: pip install -r requirements.txt")

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError:
    sys.exit("ERROR: Pillow is not installed. Run: pip install -r requirements.txt")


# =====================================================================
# SETTINGS
# =====================================================================

OUTPUT_FOLDER = Path(__file__).resolve().parent / "Images_of_image_tool"
DPI_CHOICES = (300, 450, 600, 900, 1200)
PT_PER_INCH = 72

# Dynamic DPI (high-detail, zoomable output)
DYN_MIN_DPI, DYN_MAX_DPI = 150, 300
DYN_OVERSAMPLE = 1.5            # x native picture resolution
DYN_VECTOR_DPI = 300            # pages with no embedded pictures
DYN_MAX_EDGE = 3200             # px
DYN_MIN_EDGE = 2400             # never shrink below this to meet the budget
DYN_BUDGET = 1500 * 1024        # bytes

# Safety cap for very high DPI renders (~250 MP needs ~1.5 GB RAM).
MAX_PIXELS = 250_000_000
Image.MAX_IMAGE_PIXELS = None   # local, trusted PDFs - allow big renders

# Ignore logos/icons smaller than this share of the page when reading
# the native resolution of embedded pictures.
MIN_PICTURE_AREA = 0.04

# Palette attempts for Dynamic DPI, best-looking first: (colours, dither).
PALETTE_STEPS = ((256, True), (192, True), (128, True), (256, False), (128, False))


class ToolError(Exception):
    """A problem explained to the user in plain words."""


# =====================================================================
# INPUT
# =====================================================================

def clean_path(text: str) -> str:
    """Accept drag-and-drop forms like  & 'C:\\x.pdf'  or  "C:\\x.pdf"."""
    text = text.strip()
    if text.startswith("&"):
        text = text[1:].strip()
    return text.strip('"').strip("'").strip()


def check_pdf(text: str) -> Path:
    cleaned = clean_path(text)
    if not cleaned:
        raise ToolError("Please enter a file path.")
    path = Path(cleaned).expanduser()
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as exc:
        raise ToolError(f"Cannot read file: {exc}") from exc
    if b"%PDF-" not in head:
        raise ToolError("This file is not a PDF.")
    return path


def open_pdf(path: Path) -> "pdfium.PdfDocument":
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ToolError("This PDF is password-protected.") from exc
        raise ToolError(f"The PDF could not be opened: {exc}") from exc
    if len(pdf) == 0:
        pdf.close()
        raise ToolError("This PDF has no pages.")
    return pdf


def ask(prompt: str, check):
    """Ask until the answer passes `check` (which raises ToolError if not)."""
    while True:
        try:
            return check(input(prompt))
        except ToolError as exc:
            print(f"  {exc}")


def page_checker(page_count: int):
    def check(text: str) -> int:
        text = text.strip()
        if not (text.isascii() and text.isdigit() and 1 <= int(text) <= page_count):
            raise ToolError(f"Enter a page number from 1 to {page_count}.")
        return int(text)
    return check


def option_check(text: str) -> int:
    text = text.strip()
    if text not in ("1", "2"):
        raise ToolError("Enter 1 or 2.")
    return int(text)


def dpi_check(text: str) -> int:
    text = text.strip().lower().removesuffix("dpi").strip()
    if not (text.isascii() and text.isdigit() and int(text) in DPI_CHOICES):
        raise ToolError("Enter one of: " + " / ".join(map(str, DPI_CHOICES)))
    return int(text)


# =====================================================================
# RENDERING
# =====================================================================

def native_ppi(page: "pdfium.PdfPage") -> Optional[float]:
    """Highest resolution among the page's significant embedded pictures."""
    w_pt, h_pt = page.get_size()
    page_area = max(w_pt * h_pt, 1.0)
    best = None
    try:
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            try:
                px_w, px_h = obj.get_px_size()
                m = obj.get_matrix()
            except Exception:  # noqa: BLE001 - skip unreadable objects
                continue
            ow, oh = math.hypot(m.a, m.b), math.hypot(m.c, m.d)
            if ow <= 0 or oh <= 0 or px_w < 2 or px_h < 2:
                continue
            if ow * oh / page_area < MIN_PICTURE_AREA:
                continue
            ppi = min(px_w / (ow / PT_PER_INCH), px_h / (oh / PT_PER_INCH))
            best = ppi if best is None else max(best, ppi)
    except Exception:  # noqa: BLE001 - analysis is best-effort
        return None
    return best


def dynamic_dpi(page: "pdfium.PdfPage") -> float:
    ppi = native_ppi(page)
    dpi = ppi * DYN_OVERSAMPLE if ppi else DYN_VECTOR_DPI
    dpi = min(max(dpi, DYN_MIN_DPI), DYN_MAX_DPI)
    long_edge_in = max(page.get_size()) / PT_PER_INCH
    return min(dpi, DYN_MAX_EDGE / long_edge_in)


def render(page: "pdfium.PdfPage", dpi: float) -> Image.Image:
    w_pt, h_pt = page.get_size()
    pixels = (w_pt * dpi / PT_PER_INCH) * (h_pt * dpi / PT_PER_INCH)
    if pixels > MAX_PIXELS:
        max_dpi = int(math.sqrt(MAX_PIXELS / (w_pt * h_pt)) * PT_PER_INCH)
        raise ToolError(f"This page is too large for {dpi:.0f} DPI (max about {max_dpi} DPI).")
    bitmap = page.render(
        scale=dpi / PT_PER_INCH,
        fill_color=(255, 255, 255, 255),
        draw_annots=True,
        may_draw_forms=True,
        force_bitmap_format=pdfium_c.FPDFBitmap_BGR,
        rev_byteorder=True,           # RGB straight from pdfium, no conversion
    )
    try:
        return bitmap.to_pil().copy()  # own the pixels, then free pdfium's buffer
    finally:
        bitmap.close()


def small_sample(image: Image.Image, size: int) -> Image.Image:
    """Downscaled copy for analysis - never duplicates the full-size image."""
    factor = max(1, max(image.size) // (size * 2))
    sample = image.reduce(factor) if factor > 1 else image.copy()
    sample.thumbnail((size, size))
    return sample


def is_grayscale(image: Image.Image) -> bool:
    sample = small_sample(image, 400)
    r, g, b = sample.split()
    diff = ImageChops.add(ImageChops.difference(r, g), ImageChops.difference(g, b))
    stat = ImageStat.Stat(diff)
    return stat.mean[0] < 3 and stat.extrema[0][1] < 60


def is_flat_graphic(image: Image.Image) -> bool:
    """Charts / forms / text pages: few colours, compress well losslessly."""
    sample = small_sample(image, 500)
    colors = sample.getcolors(maxcolors=4096)
    return colors is not None and len(colors) <= 512


def shrink(image: Image.Image, long_edge: int) -> Image.Image:
    if max(image.size) <= long_edge:
        return image
    s = long_edge / max(image.size)
    return image.resize((max(1, round(image.width * s)), max(1, round(image.height * s))),
                        Image.Resampling.LANCZOS, reducing_gap=3.0)


# =====================================================================
# PNG ENCODING
# =====================================================================

def png(image: Image.Image, dpi: float) -> bytes:
    buf = BytesIO()
    image.save(buf, "PNG", optimize=True, dpi=(round(dpi), round(dpi)))
    return buf.getvalue()


def png_palette(image: Image.Image, dpi: float, colors: int = 256, dither: bool = True) -> bytes:
    """Adaptive palette (pngquant-style) with optional Floyd-Steinberg dithering."""
    # Build the palette from a <=2000 px sample: same colours, far less RAM/time.
    palette = small_sample(image, 2000).quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    mapped = image.quantize(palette=palette,
                            dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)
    return png(mapped, dpi)


def exact_palette_png(image: Image.Image, dpi: float) -> Optional[bytes]:
    """Lossless palette PNG if the page has <= 256 colours."""
    if image.getcolors(maxcolors=256) is None:
        return None
    return png(image.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                              dither=Image.Dither.NONE), dpi)


def encode_fixed(image: Image.Image, dpi: float) -> Tuple[bytes, str]:
    """Select DPI: keep every pixel of the chosen DPI, compress as well as possible."""
    if image.mode == "L":
        return png(image, dpi), "grayscale"
    exact = exact_palette_png(image, dpi)
    if exact is not None:
        return exact, "lossless"
    pal = png_palette(image, dpi)
    if is_flat_graphic(image):
        full = png(image, dpi)
        if len(full) <= 1.25 * len(pal):
            return full, "lossless"
    return pal, "256 colours"


def encode_dynamic(image: Image.Image, dpi: float) -> Tuple[bytes, Image.Image, str]:
    """Dynamic DPI: best-looking PNG within DYN_BUDGET, never below DYN_MIN_EDGE."""
    while True:
        if image.mode == "L":
            data = png(image, dpi)
            if len(data) <= DYN_BUDGET:
                return data, image, "grayscale"
        else:
            exact = exact_palette_png(image, dpi)
            if exact is not None and len(exact) <= DYN_BUDGET:
                return exact, image, "lossless"

        rgb = image.convert("RGB") if image.mode == "L" else image
        smallest = None
        for i, (n, dither) in enumerate(PALETTE_STEPS):
            data = png_palette(rgb, dpi, n, dither)
            label = f"{n} colours" + ("" if dither else ", no dither")
            if smallest is None or len(data) < len(smallest[0]):
                smallest = (data, label)
            if len(data) <= DYN_BUDGET:
                if i == 0 and image.mode != "L" and is_flat_graphic(image):
                    full = png(image, dpi)
                    if len(full) <= DYN_BUDGET and len(full) <= 1.25 * len(data):
                        return full, image, "lossless"
                return data, image, label

        new_edge = int(max(image.size) * 0.85)
        if new_edge < DYN_MIN_EDGE:
            return smallest[0], image, smallest[1]
        image = shrink(image, new_edge)


# =====================================================================
# SAVE
# =====================================================================

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\(\s*welib\.org\s*\)|--.*$", "", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:60].rstrip("-") or "document"


def save(data: bytes, path: Path) -> None:
    """Write via a temp file so a crash never leaves a half-written PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".png", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        with Image.open(tmp) as im:   # verify before replacing the old file
            im.verify()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def size_text(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"


# =====================================================================
# MAIN
# =====================================================================

def main() -> int:
    try:
        pdf_path = ask("PDF file path: ", check_pdf)
        pdf = open_pdf(pdf_path)
        try:
            page_no = ask("Page number: ", page_checker(len(pdf)))
            print("\n1) Dynamic DPI")
            print("2) Select DPI")
            option = ask("Option: ", option_check)
            fixed_dpi = None
            if option == 2:
                fixed_dpi = ask("DPI (" + " / ".join(map(str, DPI_CHOICES)) + "): ", dpi_check)

            print("\nWorking...")
            page = pdf[page_no - 1]
            try:
                dpi = fixed_dpi or dynamic_dpi(page)
                image = render(page, dpi)
            finally:
                page.close()
        finally:
            pdf.close()

        if is_grayscale(image):
            image = image.convert("L")
        if fixed_dpi:
            data, how = encode_fixed(image, dpi)
            name = f"{slugify(pdf_path.stem)}_p{page_no}_{fixed_dpi}dpi.png"
        else:
            data, image, how = encode_dynamic(image, dpi)
            name = f"{slugify(pdf_path.stem)}_p{page_no}_dynamic.png"
        width, height = image.size
        del image

        out = OUTPUT_FOLDER / name
        save(data, out)

        dpi_label = f"{fixed_dpi} DPI" if fixed_dpi else f"Dynamic ({round(dpi)} DPI)"
        print(f"\nSaved: {out}")
        print(f"{width} x {height} px | {size_text(len(data))} | {dpi_label} | {how}")
        return 0

    except ToolError as exc:
        print(f"\nError: {exc}")
    except MemoryError:
        print("\nError: Not enough memory for this DPI. Choose a lower DPI.")
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
    except OSError as exc:
        print(f"\nError: File problem - {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
