#!/usr/bin/env python3
# requirements: pip install -r requirements.txt   (pypdfium2, Pillow)
"""
PDF Page -> Web-Optimised Image
===============================
Renders pages of a local PDF (atlases, textbooks, exam charts) into small,
sharp images ready to be served on the website.

Why it works the way it does
----------------------------
* Atlas pages are mostly embedded raster pictures (typically ~150 ppi JPEGs)
  plus vector text/labels. Rendering far above the embedded picture's native
  resolution only upscales it - more pixels and bytes, no extra detail. The
  tool therefore reads the native ppi of the page's pictures and renders at
  that density (vector-only pages get a sensible default).
* Output is WebP by default (supported by every current browser) and is
  encoded against a file-size budget: the highest quality that fits the
  budget is chosen automatically.
* No artificial edge-enhancement or heavy sharpening: those filters create
  halos, amplify the source JPEG's block artefacts and inflate file size.

Usage
-----
Interactive (just run it and answer the prompts):
    python pdf_page_to_jpg.py

Command line:
    python pdf_page_to_jpg.py "atlas.pdf" -p 101
    python pdf_page_to_jpg.py "atlas.pdf" -p 10-20,35 --preset zoom
    python pdf_page_to_jpg.py "atlas.pdf" -p all --format avif --max-kb 300
    python pdf_page_to_jpg.py "atlas.pdf" -p 101 --responsive

Run with --help for every option. Target: Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
except ImportError:
    print("ERROR: pypdfium2 is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from PIL import Image, ImageFilter, ImageChops, ImageStat, features
except ImportError:
    print("ERROR: Pillow is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


# =====================================================================
# CONFIGURATION
# =====================================================================

PDF_POINTS_PER_INCH = 72
DEFAULT_OUTPUT_FOLDER = Path(__file__).resolve().parent / "Images_of_Page_to_image"
MANIFEST_NAME = "manifest.json"

MIN_DPI = 72
MAX_DPI = 600
# Hard safety cap on rendered pixels (~40 MP is ~120 MB of RGB in memory).
MAX_RENDER_PIXELS = 40_000_000

# Pictures smaller than this share of the page area (logos, icons) are
# ignored when detecting the page's native resolution.
SIGNIFICANT_IMAGE_AREA_FRACTION = 0.04
# Render DPI used for pages with no significant embedded pictures.
VECTOR_PAGE_DPI = 200

# Never shrink below this long edge when fighting the size budget.
MIN_LONG_EDGE_PX = 1000


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    max_long_edge: int      # pixels
    max_kb: int             # file-size budget per image
    min_dpi: int            # floor for render density
    max_dpi: int            # ceiling for render density
    oversample: float       # render density relative to native picture ppi
    quality_max: int
    quality_min: int


PRESETS = {
    "web": Preset(
        name="web",
        description="Fast-loading page image for the website (recommended)",
        max_long_edge=2000, max_kb=450, min_dpi=110, max_dpi=220,
        oversample=1.0, quality_max=88, quality_min=60,
    ),
    "zoom": Preset(
        name="zoom",
        description="Higher detail for a zoomable viewer (larger file)",
        max_long_edge=3200, max_kb=1200, min_dpi=150, max_dpi=300,
        oversample=1.5, quality_max=90, quality_min=65,
    ),
    "thumb": Preset(
        name="thumb",
        description="Small preview / thumbnail for lists and search results",
        max_long_edge=600, max_kb=60, min_dpi=40, max_dpi=150,
        oversample=1.0, quality_max=82, quality_min=50,
    ),
}

RESPONSIVE_WIDTHS = (480, 960)   # extra srcset widths made by --responsive
FORMATS = ("webp", "avif", "jpg", "png")


# =====================================================================
# ERRORS + SMALL HELPERS
# =====================================================================

class ToolError(Exception):
    """A problem we can explain to the user in plain words."""


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def slugify(text: str, max_len: int = 60) -> str:
    """URL-safe, lowercase file-name fragment (no spaces, quotes, unicode)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\(\s*welib\.org\s*\)", "", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    text = re.sub(r"-{2,}", "-", text)
    return (text[:max_len].rstrip("-") or "document")


def safe_print(msg: str = "") -> None:
    """print() that never crashes on consoles with a limited code page."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, "replace").decode(enc))


# =====================================================================
# INPUT VALIDATION
# =====================================================================

def clean_path_input(path_str: str) -> str:
    """Accept drag-and-drop forms like  & 'C:\\x.pdf'  or  "C:\\x.pdf"."""
    path_str = path_str.strip()
    if path_str.startswith("&"):
        path_str = path_str[1:].strip()
    return path_str.strip().strip('"').strip("'").strip()


def validate_pdf_path(path_str: str) -> Path:
    path_str = clean_path_input(path_str)
    if not path_str:
        raise ToolError("No file path was given.")
    path = Path(path_str).expanduser()
    if not path.exists():
        raise ToolError(f"File not found: {path}")
    if not path.is_file():
        raise ToolError(f"This is a folder, not a file: {path}")
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as exc:
        raise ToolError(f"Unable to read file: {exc}") from exc
    # The PDF spec allows junk before the header; check the first 1 KB.
    if b"%PDF-" not in head:
        raise ToolError("This file is not a valid PDF.")
    return path


def open_pdf(path: Path, password: Optional[str]) -> "pdfium.PdfDocument":
    try:
        return pdfium.PdfDocument(str(path), password=password)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise ToolError("This PDF is password-protected. Pass it with --password.") from exc
        raise ToolError(f"The PDF could not be opened (damaged or unsupported): {exc}") from exc


def parse_page_spec(spec: str, page_count: int) -> List[int]:
    """'5' | '3-7' | '1,4,9-12' | 'all'  ->  sorted unique 1-based page numbers."""
    spec = spec.strip().lower().replace(" ", "")
    if not spec:
        raise ToolError("No page number was given.")
    if spec in {"all", "*"}:
        return list(range(1, page_count + 1))

    pages: set[int] = set()
    for part in spec.split(","):
        if not part:
            continue
        m = re.fullmatch(r"([0-9]+)(?:-([0-9]*))?", part)
        if not m:
            raise ToolError(
                f"Invalid page value '{part}'. Use e.g. 5, 3-7, 1,4,9-12 or all."
            )
        start = int(m.group(1))
        end = start if m.group(2) is None else (int(m.group(2)) if m.group(2) else page_count)
        if start < 1 or end < 1:
            raise ToolError("Page numbers start at 1.")
        if start > end:
            raise ToolError(f"Invalid range '{part}': start is after end.")
        if end > page_count:
            raise ToolError(f"Page {end} is out of range. This PDF has {page_count} pages (1-{page_count}).")
        pages.update(range(start, end + 1))
    if not pages:
        raise ToolError("No page number was given.")
    return sorted(pages)


def prepare_output_folder(folder: Path) -> Path:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"Cannot create output folder {folder}: {exc}") from exc
    if not folder.is_dir():
        raise ToolError(f"Output path is not a folder: {folder}")
    if not os.access(folder, os.W_OK):
        raise ToolError(f"Output folder is not writable: {folder}")
    return folder


def check_format_support(fmt: str) -> None:
    if fmt == "webp" and not features.check("webp"):
        raise ToolError("This Pillow build has no WebP support. Run: pip install -U Pillow")
    if fmt == "avif" and not features.check("avif"):
        raise ToolError("This Pillow build has no AVIF support. Run: pip install -U Pillow  (11.3+)")


# =====================================================================
# PAGE ANALYSIS
# =====================================================================

def native_image_ppi(page: "pdfium.PdfPage") -> Optional[float]:
    """
    Highest pixel density among the significant pictures embedded on the
    page, in pixels per inch at 100% page size. None for vector-only pages.
    """
    page_w, page_h = page.get_size()
    page_area = max(page_w * page_h, 1.0)
    best: Optional[float] = None
    try:
        objects = page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4)
        for obj in objects:
            try:
                px_w, px_h = obj.get_px_size()
                m = obj.get_matrix()
            except Exception:  # noqa: BLE001 - odd objects: just skip them
                continue
            w_pt = math.hypot(m.a, m.b)
            h_pt = math.hypot(m.c, m.d)
            if w_pt <= 0 or h_pt <= 0 or px_w <= 1 or px_h <= 1:
                continue
            if (w_pt * h_pt) / page_area < SIGNIFICANT_IMAGE_AREA_FRACTION:
                continue
            ppi = min(px_w / (w_pt / PDF_POINTS_PER_INCH), px_h / (h_pt / PDF_POINTS_PER_INCH))
            best = ppi if best is None else max(best, ppi)
    except Exception:  # noqa: BLE001 - analysis is best-effort
        return None
    return best


def choose_render_dpi(page: "pdfium.PdfPage", preset: Preset, manual_dpi: Optional[int],
                      max_long_edge: int) -> Tuple[float, Optional[float]]:
    """Return (render_dpi, native_ppi)."""
    width_pt, height_pt = page.get_size()
    long_edge_in = max(width_pt, height_pt) / PDF_POINTS_PER_INCH
    native = native_image_ppi(page)

    if manual_dpi:
        dpi = float(manual_dpi)
    else:
        base = native * preset.oversample if native else VECTOR_PAGE_DPI
        dpi = min(max(base, preset.min_dpi), preset.max_dpi)

    # Respect the long-edge limit and the memory safety cap.
    dpi = min(dpi, max_long_edge / long_edge_in)
    max_dpi_by_pixels = math.sqrt(MAX_RENDER_PIXELS / max((width_pt / 72) * (height_pt / 72), 1e-6))
    dpi = min(dpi, max_dpi_by_pixels)
    return max(dpi, 10.0), native


# =====================================================================
# RENDER + CLEAN-UP
# =====================================================================

def render_page(page: "pdfium.PdfPage", dpi: float) -> Image.Image:
    bitmap = page.render(
        scale=dpi / PDF_POINTS_PER_INCH,
        draw_annots=True,
        may_draw_forms=True,
        fill_color=(255, 255, 255, 255),
    )
    try:
        image = bitmap.to_pil()
        # to_pil() may share memory with the bitmap -> take an owned copy.
        image = image.convert("RGB")
    finally:
        bitmap.close()
    return image


def is_grayscale(image: Image.Image, tolerance: float = 3.0) -> bool:
    """True if the page has (practically) no colour."""
    sample = image.copy()
    sample.thumbnail((400, 400))
    r, g, b = sample.split()
    diff = ImageChops.add(ImageChops.difference(r, g), ImageChops.difference(g, b))
    stat = ImageStat.Stat(diff)
    return stat.mean[0] < tolerance and stat.extrema[0][1] < 60


def trim_margins(image: Image.Image, pad_fraction: float = 0.015) -> Image.Image:
    """Crop uniform near-white borders, keeping a small padding."""
    gray = image.convert("L")
    # Anything darker than 245 counts as content.
    mask = gray.point(lambda v: 255 if v < 245 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    pad = int(round(max(image.size) * pad_fraction))
    left = max(bbox[0] - pad, 0)
    top = max(bbox[1] - pad, 0)
    right = min(bbox[2] + pad, image.width)
    bottom = min(bbox[3] + pad, image.height)
    # Skip a pointless crop (less than 2% saved).
    if (right - left) * (bottom - top) > 0.98 * image.width * image.height:
        return image
    return image.crop((left, top, right, bottom))


def resize_long_edge(image: Image.Image, long_edge: int) -> Image.Image:
    if max(image.size) <= long_edge:
        return image
    scale = long_edge / max(image.size)
    new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def mild_sharpen(image: Image.Image) -> Image.Image:
    """Very gentle unsharp mask - only used when --sharpen is requested."""
    return image.filter(ImageFilter.UnsharpMask(radius=0.8, percent=45, threshold=3))


# =====================================================================
# ENCODING AGAINST A SIZE BUDGET
# =====================================================================

def encode(image: Image.Image, fmt: str, quality: int, dpi: float, lossless: bool = False) -> bytes:
    buf = BytesIO()
    d = (round(dpi), round(dpi))
    if fmt == "webp":
        if lossless:
            image.save(buf, "WEBP", lossless=True, quality=80, method=4)
        else:
            image.save(buf, "WEBP", quality=quality, method=6, exact=False)
    elif fmt == "avif":
        image.save(buf, "AVIF", quality=quality, speed=6,
                   subsampling="4:2:0" if image.mode != "L" else "4:0:0")
    elif fmt == "jpg":
        image.save(buf, "JPEG", quality=quality, optimize=True, progressive=True,
                   subsampling="4:2:0", dpi=d)
    elif fmt == "png":
        if quality < 100 and image.mode == "RGB":
            # Lossy-but-crisp: reduce to an adaptive 256-colour palette.
            pal = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                 dither=Image.Dither.FLOYDSTEINBERG)
            pal.save(buf, "PNG", optimize=True, dpi=d)
        else:
            image.save(buf, "PNG", optimize=True, compress_level=9, dpi=d)
    else:
        raise ToolError(f"Unsupported format: {fmt}")
    return buf.getvalue()


def looks_like_flat_graphic(image: Image.Image) -> bool:
    """Charts/forms/line art with few colours compress better losslessly."""
    sample = image.copy()
    sample.thumbnail((500, 500))
    colors = sample.getcolors(maxcolors=4096)
    return colors is not None and len(colors) <= 512


def encode_within_budget(image: Image.Image, fmt: str, preset: Preset, budget_bytes: int,
                         dpi: float, fixed_quality: Optional[int]) -> Tuple[bytes, Image.Image, str]:
    """
    Find the best-looking encoding that fits the budget.
    Returns (data, final_image, description_of_encoding).
    """
    if fmt == "png":
        data = encode(image, "png", 100, dpi)
        if len(data) > budget_bytes and fixed_quality is None:
            data = encode(image, "png", 90, dpi)   # palette PNG
            return data, image, "png-256"
        return data, image, "png"

    current = image
    while True:
        candidates: List[Tuple[bytes, str]] = []

        if fixed_quality is not None:
            return encode(current, fmt, fixed_quality, dpi), current, f"q{fixed_quality}"

        if fmt == "webp" and looks_like_flat_graphic(current):
            lossless = encode(current, "webp", 100, dpi, lossless=True)
            if len(lossless) <= budget_bytes:
                candidates.append((lossless, "lossless"))

        # Binary search: highest quality whose output fits the budget.
        lo, hi = preset.quality_min, preset.quality_max
        best: Optional[Tuple[bytes, str]] = None
        top = encode(current, fmt, hi, dpi)
        if len(top) <= budget_bytes:
            best = (top, f"q{hi}")
        else:
            while lo <= hi:
                mid = (lo + hi) // 2
                data = encode(current, fmt, mid, dpi)
                if len(data) <= budget_bytes:
                    best = (data, f"q{mid}")
                    lo = mid + 1
                else:
                    hi = mid - 1
        if best:
            candidates.append(best)

        if candidates:
            # Lossless keeps text perfect: use it when it fits and costs at
            # most ~25% more than the best lossy version; otherwise lossy.
            lossless_c = [c for c in candidates if c[1] == "lossless"]
            lossy_c = [c for c in candidates if c[1] != "lossless"]
            if lossless_c and (not lossy_c or len(lossless_c[0][0]) <= 1.25 * len(lossy_c[0][0])):
                return lossless_c[0][0], current, "lossless"
            chosen = lossy_c[0] if lossy_c else lossless_c[0]
            return chosen[0], current, chosen[1]

        # Still too big at minimum quality -> shrink a little and retry.
        new_long = int(max(current.size) * 0.85)
        if new_long < min(MIN_LONG_EDGE_PX, preset.max_long_edge):
            data = encode(current, fmt, preset.quality_min, dpi)
            return data, current, f"q{preset.quality_min} (over budget)"
        current = resize_long_edge(current, new_long)


# =====================================================================
# SAVING
# =====================================================================

def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file so a crash never leaves a half-written image."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=path.suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def verify_image_bytes(data: bytes) -> Tuple[int, int]:
    try:
        with Image.open(BytesIO(data)) as im:
            im.verify()
        with Image.open(BytesIO(data)) as im:
            im.load()
            return im.size
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Encoded image failed verification: {exc}") from exc


def update_manifest(folder: Path, entries: Sequence[dict]) -> Path:
    path = folder / MANIFEST_NAME
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}
    images = existing.get("images", {})
    for e in entries:
        images[e["file"]] = e
    existing["images"] = dict(sorted(images.items()))
    atomic_write(path, json.dumps(existing, indent=2, ensure_ascii=False).encode("utf-8"))
    return path


@dataclass
class PageResult:
    file: str
    source_pdf: str
    page: int
    width: int
    height: int
    bytes: int
    format: str
    encoding: str
    render_dpi: int
    native_ppi: Optional[int]
    srcset: Optional[List[dict]] = None


# =====================================================================
# MAIN CONVERSION
# =====================================================================

@dataclass
class Options:
    pdf_path: Path
    pages_spec: str
    preset: Preset
    fmt: str = "webp"
    max_kb: Optional[int] = None
    max_edge: Optional[int] = None
    dpi: Optional[int] = None
    quality: Optional[int] = None
    trim: bool = False
    sharpen: bool = False
    responsive: bool = False
    output: Path = DEFAULT_OUTPUT_FOLDER
    password: Optional[str] = None
    manifest: bool = True


def output_name(pdf_path: Path, page_number: int, page_count: int, preset: Preset,
                fmt: str, width: Optional[int] = None) -> str:
    digits = max(3, len(str(page_count)))
    suffix = "" if preset.name == "web" else f"_{preset.name}"
    w = f"_{width}w" if width else ""
    return f"{slugify(pdf_path.stem)}_p{page_number:0{digits}d}{suffix}{w}.{fmt}"


def convert_page(pdf: "pdfium.PdfDocument", page_number: int, opts: Options) -> PageResult:
    preset = opts.preset
    max_edge = opts.max_edge or preset.max_long_edge
    budget = (opts.max_kb or preset.max_kb) * 1024
    page = pdf[page_number - 1]
    try:
        dpi, native = choose_render_dpi(page, preset, opts.dpi, max_edge)
        try:
            image = render_page(page, dpi)
        except MemoryError as exc:
            raise ToolError("Out of memory while rendering. Use a lower --dpi or --max-edge.") from exc
    finally:
        page.close()

    if opts.trim:
        image = trim_margins(image)
    image = resize_long_edge(image, max_edge)  # in case trim/DPI rounding exceeded it
    if is_grayscale(image):
        image = image.convert("L")
    if opts.sharpen:
        image = mild_sharpen(image)

    data, final_img, enc_label = encode_within_budget(image, opts.fmt, preset, budget, dpi, opts.quality)
    width, height = verify_image_bytes(data)

    page_count = len(pdf)
    name = output_name(opts.pdf_path, page_number, page_count, preset, opts.fmt)
    atomic_write(opts.output / name, data)

    srcset = None
    if opts.responsive:
        srcset = []
        for w in RESPONSIVE_WIDTHS:
            if w >= width:
                continue
            scale = w / final_img.width
            small = final_img.resize((w, max(1, round(final_img.height * scale))),
                                     Image.Resampling.LANCZOS, reducing_gap=3.0)
            small_budget = max(int(budget * (w / width) ** 2 * 1.3), 25 * 1024)
            s_data, _, _ = encode_within_budget(small, opts.fmt, preset, small_budget, dpi, opts.quality)
            sw, sh = verify_image_bytes(s_data)
            s_name = output_name(opts.pdf_path, page_number, page_count, preset, opts.fmt, sw)
            atomic_write(opts.output / s_name, s_data)
            srcset.append({"file": s_name, "width": sw, "height": sh, "bytes": len(s_data)})
        srcset.append({"file": name, "width": width, "height": height, "bytes": len(data)})

    return PageResult(
        file=name, source_pdf=opts.pdf_path.name, page=page_number,
        width=width, height=height, bytes=len(data), format=opts.fmt,
        encoding=enc_label, render_dpi=round(dpi),
        native_ppi=round(native) if native else None, srcset=srcset,
    )


def html_snippet(r: PageResult, alt: str) -> str:
    alt = alt.replace('"', "&quot;")
    if r.srcset and len(r.srcset) > 1:
        srcset = ", ".join(f"{s['file']} {s['width']}w" for s in r.srcset)
        return (f'<img src="{r.file}" srcset="{srcset}" sizes="(max-width: 1000px) 100vw, 1000px" '
                f'width="{r.width}" height="{r.height}" alt="{alt}" loading="lazy" decoding="async">')
    return (f'<img src="{r.file}" width="{r.width}" height="{r.height}" alt="{alt}" '
            f'loading="lazy" decoding="async">')


def run(opts: Options) -> int:
    check_format_support(opts.fmt)
    prepare_output_folder(opts.output)
    pdf = open_pdf(opts.pdf_path, opts.password)
    results: List[PageResult] = []
    failures: List[Tuple[int, str]] = []
    try:
        page_count = len(pdf)
        if page_count == 0:
            raise ToolError("This PDF has no pages.")
        pages = parse_page_spec(opts.pages_spec, page_count)
        budget_kb = opts.max_kb or opts.preset.max_kb
        safe_print(f"\nPDF: {opts.pdf_path.name}  ({page_count} pages)")
        safe_print(f"Preset: {opts.preset.name} | format: {opts.fmt} | budget: {budget_kb} KB per image")
        safe_print(f"Output folder: {opts.output}\n")

        started = time.time()
        for i, page_number in enumerate(pages, 1):
            t0 = time.time()
            prefix = f"[{i}/{len(pages)}] page {page_number}"
            try:
                r = convert_page(pdf, page_number, opts)
            except (ToolError, OSError, pdfium.PdfiumError, MemoryError) as exc:
                failures.append((page_number, str(exc)))
                safe_print(f"{prefix}: FAILED - {exc}")
                continue
            results.append(r)
            native = f"native {r.native_ppi} ppi" if r.native_ppi else "vector page"
            extra = f" (+{len(r.srcset) - 1} srcset sizes)" if r.srcset and len(r.srcset) > 1 else ""
            safe_print(f"{prefix}: {r.file}  {r.width}x{r.height}px  {format_size(r.bytes)}  "
                       f"[{r.encoding}, {r.render_dpi} dpi, {native}]{extra}  {time.time() - t0:.1f}s")

        if results and opts.manifest:
            update_manifest(opts.output, [asdict(r) for r in results])

        total = sum(r.bytes for r in results)
        safe_print()
        safe_print(f"Done: {len(results)} image(s), {format_size(total)} total, "
                   f"{time.time() - started:.1f}s.")
        if len(results) == 1:
            r = results[0]
            safe_print("\nHTML (width/height prevent layout shift):")
            title = re.sub(r"\(\s*welib\.org\s*\)|--.*$", "", opts.pdf_path.stem, flags=re.I)
            title = re.sub(r"[_\s]+", " ", title).strip() or opts.pdf_path.stem
            safe_print(html_snippet(r, f"{title} - page {r.page}"))
        if failures:
            safe_print(f"\n{len(failures)} page(s) failed: " + ", ".join(str(p) for p, _ in failures))
            return 2
        return 0
    finally:
        pdf.close()


# =====================================================================
# INTERACTIVE MODE + CLI
# =====================================================================

def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else (default or "")


def ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        v = input(f"{prompt} [{d}]: ").strip().lower()
        if not v:
            return default
        if v in {"y", "yes"}:
            return True
        if v in {"n", "no"}:
            return False
        print("  Please answer y or n.")


def interactive() -> Options:
    safe_print("=" * 56)
    safe_print("        PDF Page -> Web-Optimised Image")
    safe_print("=" * 56)
    safe_print("Tip: drag the PDF into this window to paste its path.\n")

    while True:
        try:
            pdf_path = validate_pdf_path(ask("PDF file path"))
            break
        except ToolError as exc:
            safe_print(f"  {exc}")

    safe_print("\nPages: a number (101), a range (10-20), a list (5,9,12-14) or 'all'.")
    safe_print("Note: this is the PDF page position, not the number printed on the page.")
    pages_spec = ask("Pages")

    safe_print("\nOutput preset:")
    names = list(PRESETS)
    for i, n in enumerate(names, 1):
        p = PRESETS[n]
        safe_print(f"  {i}. {n:<5} - {p.description} (<= {p.max_kb} KB, <= {p.max_long_edge}px)")
    while True:
        choice = ask("Choose preset", "1").lower()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            preset = PRESETS[names[int(choice) - 1]]
            break
        if choice in PRESETS:
            preset = PRESETS[choice]
            break
        safe_print("  Please enter one of the numbers above.")

    while True:
        fmt = ask("Format (webp / avif / jpg / png)", "webp").lower().lstrip(".")
        fmt = "jpg" if fmt == "jpeg" else fmt
        if fmt in FORMATS:
            break
        safe_print("  Please choose webp, avif, jpg or png.")

    trim = ask_yes_no("Trim empty white margins?", False)
    responsive = ask_yes_no("Also make smaller sizes for phones (srcset)?", False)
    return Options(pdf_path=pdf_path, pages_spec=pages_spec, preset=preset, fmt=fmt,
                   trim=trim, responsive=responsive)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Render PDF pages into small, sharp, web-ready images.",
        epilog="Run without arguments for interactive mode.",
    )
    p.add_argument("pdf", nargs="?", help="Path to the PDF file")
    p.add_argument("-p", "--pages", help="Pages: 5 | 3-7 | 1,4,9-12 | all (PDF page position, 1-based)")
    p.add_argument("--preset", choices=list(PRESETS), default="web", help="Output preset (default: web)")
    p.add_argument("-f", "--format", choices=FORMATS, default="webp", help="Image format (default: webp)")
    p.add_argument("--max-kb", type=int, help="File-size budget per image in KB (overrides preset)")
    p.add_argument("--max-edge", type=int, help="Maximum long edge in pixels (overrides preset)")
    p.add_argument("--dpi", type=int, help=f"Force render DPI ({MIN_DPI}-{MAX_DPI}) instead of auto")
    p.add_argument("--quality", type=int, help="Force a fixed quality 1-100 (disables the size budget)")
    p.add_argument("--trim", action="store_true", help="Crop empty white margins")
    p.add_argument("--sharpen", action="store_true", help="Apply a very mild sharpen")
    p.add_argument("--responsive", action="store_true",
                   help=f"Also write {', '.join(map(str, RESPONSIVE_WIDTHS))}px-wide versions for srcset")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT_FOLDER, help="Output folder")
    p.add_argument("--password", help="Password for encrypted PDFs")
    p.add_argument("--no-manifest", action="store_true", help=f"Do not update {MANIFEST_NAME}")
    return p


def options_from_args(args: argparse.Namespace) -> Options:
    pdf_path = validate_pdf_path(args.pdf)
    if not args.pages:
        raise ToolError("Please give the pages with -p/--pages (e.g. -p 101 or -p 10-20).")
    if args.dpi is not None and not (MIN_DPI <= args.dpi <= MAX_DPI):
        raise ToolError(f"--dpi must be between {MIN_DPI} and {MAX_DPI}.")
    if args.quality is not None and not (1 <= args.quality <= 100):
        raise ToolError("--quality must be between 1 and 100.")
    if args.max_kb is not None and args.max_kb < 10:
        raise ToolError("--max-kb must be at least 10.")
    if args.max_edge is not None and not (200 <= args.max_edge <= 10000):
        raise ToolError("--max-edge must be between 200 and 10000.")
    return Options(
        pdf_path=pdf_path, pages_spec=args.pages, preset=PRESETS[args.preset], fmt=args.format,
        max_kb=args.max_kb, max_edge=args.max_edge, dpi=args.dpi, quality=args.quality,
        trim=args.trim, sharpen=args.sharpen, responsive=args.responsive,
        output=args.output.expanduser().resolve(), password=args.password,
        manifest=not args.no_manifest,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        opts = options_from_args(args) if args.pdf else interactive()
        return run(opts)
    except ToolError as exc:
        safe_print(f"\nError: {exc}")
        return 1
    except KeyboardInterrupt:
        safe_print("\nCancelled by user.")
        return 130
    except EOFError:
        safe_print("\nNo input received - cancelled.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
