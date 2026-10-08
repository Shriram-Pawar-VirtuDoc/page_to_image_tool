#!/usr/bin/env python3
# requirements: pip install -r requirements.txt   (pypdfium2, Pillow)
"""
Image Tool - render one PDF page to a compact PNG.

Run:  python image_tool.py
Then enter the PDF path, the page number (PDF page position, 1 = first page)
and choose:
  1) Dynamic DPI - at least 450 DPI, pushed as high as possible (up to 900 DPI)
     while the PNG stays under 4 MB. Use this by default.
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

# Dynamic DPI (option 1): at least DYN_MIN_DPI, as high as the size budget allows.
DYN_MIN_DPI = 450               # never render below this
DYN_MAX_DPI = 900               # upper limit; atlas drawings are stored at ~150 ppi,
                                # so higher only sharpens text and lines
DYN_STEP = 25                   # DPI search step
DYN_BUDGET = 4_000_000          # bytes; every option-1 PNG stays strictly below 4 MB
DYN_SIZE_EXPONENT = 1.6         # PNG size grows about DPI^1.6 on atlas pages (measured)

# Safety cap for very high DPI renders (~250 MP needs ~1.5 GB RAM).
MAX_PIXELS = 250_000_000
Image.MAX_IMAGE_PIXELS = None   # local, trusted PDFs - allow big renders

# Palette attempts, best-looking first: (colours, dither).
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


def encode_within_budget(image: Image.Image, dpi: float,
                         max_step: int = len(PALETTE_STEPS) - 1) -> Optional[Tuple[bytes, str, int]]:
    """Best-looking PNG of this exact image (no resizing) that is under DYN_BUDGET.

    Returns (png, description, quality step) or None. Step -1 is lossless; steps 0..n are
    PALETTE_STEPS, best first. max_step stops it trading colour quality for size.
    """
    if image.mode == "L":
        data = png(image, dpi)
        if len(data) < DYN_BUDGET:
            return data, "grayscale", -1
        rgb = image.convert("RGB")
    else:
        exact = exact_palette_png(image, dpi)
        if exact is not None and len(exact) < DYN_BUDGET:
            return exact, "lossless", -1
        rgb = image
    for i, (n, dither) in enumerate(PALETTE_STEPS[:max_step + 1]):
        data = png_palette(rgb, dpi, n, dither)
        if len(data) < DYN_BUDGET:
            if i == 0 and image.mode != "L" and is_flat_graphic(image):
                full = png(image, dpi)
                if len(full) < DYN_BUDGET and len(full) <= 1.25 * len(data):
                    return full, "lossless", -1
            return data, f"{n} colours" + ("" if dither else ", no dither"), i
    return None


def _render_within_budget(page: "pdfium.PdfPage", dpi: float, max_step: int = len(PALETTE_STEPS) - 1):
    image = render(page, dpi)
    if is_grayscale(image):
        image = image.convert("L")
    size = image.size
    result = encode_within_budget(image, dpi, max_step)
    del image
    return result, size


def render_dynamic(page: "pdfium.PdfPage") -> Tuple[bytes, int, Tuple[int, int], str]:
    """Option 1: the highest DPI from DYN_MIN_DPI to DYN_MAX_DPI whose PNG is under DYN_BUDGET.

    Renders once at DYN_MIN_DPI, predicts the highest DPI that should still fit, then steps
    down by DYN_STEP until one fits. A higher DPI is only accepted at the same or better
    colour quality than the DYN_MIN_DPI render, so sharpness is never bought with banding.
    Returns (png, dpi, (width, height), description).
    """
    probe, size = _render_within_budget(page, DYN_MIN_DPI)
    if probe is None:
        raise ToolError(f"This page cannot be saved under {DYN_BUDGET / 1e6:.0f} MB even at "
                        f"{DYN_MIN_DPI} DPI. Use option 2 with a lower DPI.")
    data, how, step = probe
    best = (data, DYN_MIN_DPI, size, how)
    target = DYN_MIN_DPI * (DYN_BUDGET * 0.97 / len(data)) ** (1 / DYN_SIZE_EXPONENT)
    dpi = min(DYN_MAX_DPI, int(target // DYN_STEP) * DYN_STEP)
    while dpi > DYN_MIN_DPI:
        result, size = _render_within_budget(page, dpi, step)
        if result is not None and result[2] <= step:
            return result[0], dpi, size, result[1]
        dpi -= DYN_STEP
    return best


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
                if fixed_dpi:
                    dpi = fixed_dpi
                    image = render(page, dpi)
                else:
                    data, dpi, (width, height), how = render_dynamic(page)
            finally:
                page.close()
        finally:
            pdf.close()

        if fixed_dpi:
            if is_grayscale(image):
                image = image.convert("L")
            data, how = encode_fixed(image, dpi)
            width, height = image.size
            del image
            name = f"{slugify(pdf_path.stem)}_p{page_no}_{fixed_dpi}dpi.png"
        else:
            name = f"{slugify(pdf_path.stem)}_p{page_no}_dynamic.png"

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
