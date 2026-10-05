#!/usr/bin/env python3
# requirements: pip install pypdfium2 Pillow
"""
 PDF Page to High-Quality PNG Converter
---------------------------------------
Converts a single page of a local PDF into a high-quality PNG image.

- Rendering: pypdfium2
- Image processing / optimization: Pillow
- Everything else: Python standard library only

No OpenCV, no NumPy, no AI super-resolution, no cloud APIs, no upscaling.
Target: Python 3.11+
"""

from __future__ import annotations

import sys
import time
import threading
import itertools
import math
from pathlib import Path
from typing import Optional, Tuple

try:
    import pypdfium2 as pdfium
except ImportError:
    print("ERROR: pypdfium2 is not installed. Run: pip install pypdfium2")
    sys.exit(1)

try:
    from PIL import Image, ImageFilter, ImageOps, ImageStat
except ImportError:
    print("ERROR: Pillow is not installed. Run: pip install Pillow")
    sys.exit(1)


# =====================================================================
# CONFIGURATION
# =====================================================================

DEFAULT_DPI = 600
MIN_DPI = 150
MAX_DPI = 1200

# Zoom-safe rendering tries to keep enough pixel density for deep zoom.
ZOOM_SAFE_MIN_LONG_EDGE_PX = 3200
ZOOM_SAFE_MAX_DPI = 1200

# Dynamic mode keeps output dimensions practical while preserving detail.
DYNAMIC_DPI_LOW = 200
DYNAMIC_DPI_MID = 300
DYNAMIC_DPI_HIGH = 450
DYNAMIC_DPI_MAX = DYNAMIC_DPI_HIGH

# Preview render used only for complexity analysis (kept small/fast on purpose)
PREVIEW_DPI = 46
PDFIUM_BASE_DPI = 72

DEFAULT_OUTPUT_FOLDER = Path(__file__).resolve().parent / "Images_of_Page_to_image"

PNG_COMPRESS_LEVEL = 9

# Unsharp mask defaults (kept configurable)
UNSHARP_RADIUS = 1.5
UNSHARP_PERCENT = 120
UNSHARP_THRESHOLD = 3

# Median filter size for conservative noise reduction
MEDIAN_FILTER_SIZE = 3

# Complexity score thresholds (0.0 - 1.0 scale) that decide dynamic DPI
COMPLEXITY_LOW_THRESHOLD = 0.33
COMPLEXITY_MID_THRESHOLD = 0.66


# =====================================================================
# SPINNER (live progress in a background thread)
# =====================================================================

class Spinner:
    """Simple animated terminal spinner with percentage and ETA."""

    SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self._lock = threading.Lock()
        self._percent = 0
        self._message = "Starting..."
        self._start_time = time.time()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, percent: int, message: str = ""):
        with self._lock:
            self._percent = max(0, min(100, percent))
            if message:
                self._message = message

    def _run(self):
        cycle = itertools.cycle(self.SPINNER_CHARS)
        while not self._stop_event.is_set():
            with self._lock:
                percent = self._percent
                message = self._message
            elapsed = time.time() - self._start_time
            remaining = self._estimate_remaining(percent, elapsed)
            line = f"\r{next(cycle)} {message}... {percent}% | Remaining: {remaining}"
            sys.stdout.write(line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.1)

    @staticmethod
    def _estimate_remaining(percent: int, elapsed: float) -> str:
        if percent <= 0:
            remaining_seconds = 0
        else:
            total_estimated = elapsed / (percent / 100.0)
            remaining_seconds = max(0, total_estimated - elapsed)
        minutes, seconds = divmod(int(remaining_seconds), 60)
        return f"{minutes:02d}:{seconds:02d}"

    def stop(self, success: bool = True):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            percent = self._percent
            message = self._message
        final_symbol = "✔" if success else "✘"
        sys.stdout.write(f"\r{final_symbol} {message}... {percent}%".ljust(80) + "\n")
        sys.stdout.flush()


# =====================================================================
# VALIDATION FUNCTIONS
# =====================================================================

class ValidationError(Exception):
    """Raised when user-provided input fails validation."""


def validate_pdf_path(path_str: str) -> Path:
    path_str = path_str.strip()

    # Accept PowerShell drag-and-drop form like:
    # & 'C:\path\file.pdf'
    if path_str.startswith("&"):
        path_str = path_str[1:].strip()

    path_str = path_str.strip().strip('"').strip("'")
    if not path_str:
        raise ValidationError("Incorrect file path")

    path = Path(path_str)

    if not path.exists():
        raise ValidationError("Incorrect file path")

    if not path.is_file():
        raise ValidationError("Incorrect file path")

    if path.suffix.lower() != ".pdf":
        raise ValidationError("File format is Invalid")

    try:
        with open(path, "rb") as f:
            header = f.read(5)
    except OSError as exc:
        raise ValidationError(f"Unable to read file: {exc}")

    if header != b"%PDF-":
        raise ValidationError("File format is Invalid")

    return path


def validate_output_folder(folder: Path) -> Path:
    if not folder.exists():
        raise ValidationError(
            f"Default output folder does not exist:\n{folder}\n"
            "Please create this folder and try again."
        )

    if not folder.is_dir():
        raise ValidationError(f"Output path is not a folder:\n{folder}")

    test_file = folder / ".write_test.tmp"
    try:
        with open(test_file, "wb") as f:
            f.write(b"test")
        test_file.unlink(missing_ok=True)
    except OSError:
        raise ValidationError(f"Output folder is not writable:\n{folder}")

    return folder


def validate_page_number(value: str, page_count: int) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValidationError(
            f"Page number must be a positive integer. Valid range: 1-{page_count}"
        )

    page_number = int(value)

    if page_number <= 0:
        raise ValidationError(
            f"Page number must be greater than zero. Valid range: 1-{page_count}"
        )

    if page_number > page_count:
        raise ValidationError(
            f"Page number out of range. Valid range: 1-{page_count}"
        )

    return page_number


def validate_dpi(value: str) -> int:
    value = value.strip()
    if not value:
        return DEFAULT_DPI

    if not value.isdigit():
        raise ValidationError(f"DPI must be an integer between {MIN_DPI} and {MAX_DPI}")

    dpi = int(value)

    if dpi < MIN_DPI or dpi > MAX_DPI:
        raise ValidationError(f"DPI must be between {MIN_DPI} and {MAX_DPI}")

    return dpi


def validate_dpi_mode(value: str) -> bool:
    """
    Returns True for dynamic mode, False for manual mode.
    Accepts numeric and text input for convenience.
    """
    normalized = value.strip().lower()
    if normalized in {"1", "dynamic", "d"}:
        return True
    if normalized in {"2", "manual", "m"}:
        return False
    raise ValidationError("Invalid DPI mode selection. Choose 1/Dynamic or 2/Manual.")


def validate_yes_no(value: str, default: bool = True) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"y", "yes", "true", "1"}:
        return True
    if normalized in {"n", "no", "false", "0"}:
        return False
    raise ValidationError("Invalid choice. Enter Y or N.")


# =====================================================================
# PDF RENDERING
# =====================================================================

def render_preview(pdf: "pdfium.PdfDocument", page_index: int) -> Image.Image:
    """Render a lightweight low-resolution preview for complexity analysis."""
    page = pdf[page_index]
    try:
        scale = PREVIEW_DPI / PDFIUM_BASE_DPI
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil().convert("RGB")
        return pil_image
    finally:
        page.close()


def render_page(pdf: "pdfium.PdfDocument", page_index: int, dpi: int) -> Image.Image:
    """Render the requested page at the final selected DPI."""
    page = pdf[page_index]
    try:
        scale = dpi / PDFIUM_BASE_DPI
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil().convert("RGB")
        return pil_image
    finally:
        page.close()


def estimate_page_long_edge_pixels(pdf: "pdfium.PdfDocument", page_index: int, dpi: int) -> int:
    """Estimate long-edge pixels from page size and target DPI."""
    page = pdf[page_index]
    try:
        width_pt, height_pt = page.get_size()
    finally:
        page.close()
    long_edge_pt = max(width_pt, height_pt)
    return int(round(long_edge_pt * (dpi / PDFIUM_BASE_DPI)))


def compute_effective_dpi(
    base_dpi: int,
    zoom_safe_enabled: bool,
    page_long_edge_px: int,
    max_dpi: int = ZOOM_SAFE_MAX_DPI,
) -> int:
    """
    Returns the actual rendering DPI.
    In zoom-safe mode, lift DPI so long-edge pixels stay above threshold.
    """
    if not zoom_safe_enabled:
        return base_dpi

    if page_long_edge_px >= ZOOM_SAFE_MIN_LONG_EDGE_PX:
        return base_dpi

    scale = ZOOM_SAFE_MIN_LONG_EDGE_PX / max(page_long_edge_px, 1)
    boosted_dpi = int(math.ceil(base_dpi * scale))
    return min(max(boosted_dpi, base_dpi), max_dpi)


# =====================================================================
# COMPLEXITY ANALYSIS / DYNAMIC DPI SELECTION
# =====================================================================

def analyze_page_complexity(preview: Image.Image) -> float:
    """
    Estimate page complexity on a 0.0-1.0 scale using cheap Pillow-only
    signals: edge density (via an edge filter) and color/tonal variation.
    """
    grayscale = preview.convert("L")

    # Edge density: run an edge-detection filter and measure how much of
    # the image contains strong edges (proxy for line/diagram density).
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    edge_mean = edge_stat.mean[0]  # 0-255
    edge_density = min(edge_mean / 60.0, 1.0)  # normalize, empirically tuned

    # Tonal variation: standard deviation of grayscale pixel values.
    tonal_stat = ImageStat.Stat(grayscale)
    tonal_stddev = tonal_stat.stddev[0]  # 0-~128
    tonal_variation = min(tonal_stddev / 80.0, 1.0)

    # Color variation: number of distinct colors relative to pixel count
    # (capped sample via getcolors; None result means highly varied/complex).
    small = preview.resize((min(preview.width, 200), min(preview.height, 200)))
    colors = small.getcolors(maxcolors=100000)
    if colors is None:
        color_variation = 1.0
    else:
        distinct = len(colors)
        total_pixels = small.width * small.height
        color_variation = min((distinct / max(total_pixels, 1)) * 4, 1.0)

    # Weighted combination: edges matter most for diagrams/technical drawings.
    score = (edge_density * 0.5) + (tonal_variation * 0.25) + (color_variation * 0.25)
    return round(min(max(score, 0.0), 1.0), 4)


def select_dynamic_dpi(complexity_score: float) -> int:
    if complexity_score < COMPLEXITY_LOW_THRESHOLD:
        return DYNAMIC_DPI_LOW
    elif complexity_score < COMPLEXITY_MID_THRESHOLD:
        return DYNAMIC_DPI_MID
    else:
        return DYNAMIC_DPI_HIGH


def select_dynamic_dpi_for_page(
    complexity_score: float,
    page_width_pt: float,
    page_height_pt: float,
) -> int:
    """Choose DPI from page detail and physical size, with bounded output."""
    base_dpi = select_dynamic_dpi(complexity_score)
    long_edge_inches = max(page_width_pt, page_height_pt) / PDFIUM_BASE_DPI

    # Small pages need more DPI to retain fine print; large pages reach the
    # same useful pixel density at a lower DPI and otherwise become enormous.
    if long_edge_inches <= 8.5:
        size_adjustment = 150
    elif long_edge_inches >= 17:
        size_adjustment = -150
    else:
        size_adjustment = 0

    return min(max(base_dpi + size_adjustment, MIN_DPI), DYNAMIC_DPI_MAX)


# =====================================================================
# IMAGE OPTIMIZATION (PILLOW)
# =====================================================================

def _needs_denoise(image: Image.Image) -> bool:
    """
    Conservative heuristic: only denoise if the image shows signs of
    noise/scan artifacts (relatively high local variance in a grayscale
    sample), so clean, crisp diagrams are not unnecessarily blurred.
    """
    grayscale = image.convert("L")
    stat = ImageStat.Stat(grayscale)
    stddev = stat.stddev[0]
    # High stddev alone isn't noise; combine with a quick edge-noise proxy.
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    # Very high edge mean combined with high stddev suggests speckle/noise
    # rather than clean diagram lines.
    return edge_stat.mean[0] > 40 and stddev > 55


def optimize_image(
    image: Image.Image,
    unsharp_radius: float = UNSHARP_RADIUS,
    unsharp_percent: int = UNSHARP_PERCENT,
    unsharp_threshold: int = UNSHARP_THRESHOLD,
) -> Image.Image:
    """
    Conservative Pillow-only optimization pipeline:
    conditional denoise -> edge enhance -> controlled sharpen ->
    conditional autocontrast.
    """
    working = image

    # Conditional noise reduction (mild, conservative).
    if _needs_denoise(working):
        working = working.filter(ImageFilter.MedianFilter(size=MEDIAN_FILTER_SIZE))

    # Edge enhancement to strengthen diagram boundaries and fine lines.
    working = working.filter(ImageFilter.EDGE_ENHANCE_MORE)

    # Controlled sharpening via unsharp mask.
    working = working.filter(
        ImageFilter.UnsharpMask(
            radius=unsharp_radius,
            percent=unsharp_percent,
            threshold=unsharp_threshold,
        )
    )

    # Conditional contrast optimization: only apply autocontrast when the
    # image is relatively low-contrast (avoid altering already-good images).
    grayscale_stat = ImageStat.Stat(working.convert("L"))
    stddev = grayscale_stat.stddev[0]
    if stddev < 45:
        working = ImageOps.autocontrast(working, cutoff=1)

    return working


# =====================================================================
# SAVE + VERIFY
# =====================================================================

def build_output_filename(pdf_path: Path, page_number: int, selected_dpi: int, dpi_mode: str) -> str:
    pdf_stem = pdf_path.stem
    return f"Pagenum_{page_number}_{selected_dpi}_{dpi_mode}_{pdf_stem}.png"


def save_image(image: Image.Image, output_path: Path, dpi: int) -> None:
    if image.mode != "RGB":
        image = image.convert("RGB")

    image.save(
        output_path,
        format="PNG",
        compress_level=PNG_COMPRESS_LEVEL,
        optimize=True,
        dpi=(dpi, dpi),
    )


def verify_output_file(output_path: Path, expected_dpi: int) -> Tuple[int, int, int]:
    if not output_path.exists():
        raise ValidationError("Output verification failed: file does not exist")

    if not output_path.is_file():
        raise ValidationError("Output verification failed: not a regular file")

    file_size = output_path.stat().st_size
    if file_size <= 0:
        raise ValidationError("Output verification failed: file size is zero")

    if output_path.suffix.lower() != ".png":
        raise ValidationError("Output verification failed: incorrect extension")

    try:
        with Image.open(output_path) as reopened:
            reopened.verify()
        with Image.open(output_path) as reopened:
            if reopened.mode != "RGB":
                raise ValidationError("Output verification failed: image mode is not RGB")
            width, height = reopened.size
            if width <= 0 or height <= 0:
                raise ValidationError("Output verification failed: invalid dimensions")

            dpi_info = reopened.info.get("dpi")
            if dpi_info:
                saved_dpi = round(dpi_info[0])
                if abs(saved_dpi - expected_dpi) > 1:
                    raise ValidationError(
                        "Output verification failed: DPI metadata mismatch "
                        f"(expected {expected_dpi}, found {saved_dpi})"
                    )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"Output verification failed: Pillow could not reopen image ({exc})")

    return width, height, file_size


def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


# =====================================================================
# MAIN APPLICATION FLOW
# =====================================================================

def print_header():
    print("=" * 50)
    print("       PDF Page to High-Quality PNG Converter")
    print("=" * 50)
    print()


def prompt_pdf_path() -> str:
    return input("Enter local PDF file path: ")


def prompt_page_number() -> str:
    return input("Enter PDF page number: ")


def prompt_dpi_mode() -> str:
    print()
    print("DPI Mode:")
    print(f"1. Dynamic DPI (auto: {DYNAMIC_DPI_LOW}/{DYNAMIC_DPI_MID}/{DYNAMIC_DPI_HIGH})")
    print(f"2. Manual DPI ({MIN_DPI}-{MAX_DPI})")
    print()
    return input("Select DPI mode: ").strip()


def prompt_manual_dpi() -> str:
    return input(f"Enter DPI ({MIN_DPI}-{MAX_DPI}): ")


def prompt_zoom_safe_mode() -> bool:
    return True


def main() -> int:
    print_header()

    spinner = Spinner()

    try:
        # ---- Input validation (5%) ----
        pdf_path_input = prompt_pdf_path()
        pdf_path = validate_pdf_path(pdf_path_input)

        output_folder = validate_output_folder(DEFAULT_OUTPUT_FOLDER)

        pdf = None
        try:
            pdf = pdfium.PdfDocument(str(pdf_path))
            page_count = len(pdf)

            page_number_input = prompt_page_number()
            page_number = validate_page_number(page_number_input, page_count)
            page_index = page_number - 1

            dpi_mode_input = prompt_dpi_mode()
            use_dynamic = validate_dpi_mode(dpi_mode_input)
            if use_dynamic:
                selected_dpi = None
            else:
                manual_dpi_input = prompt_manual_dpi()
                selected_dpi = validate_dpi(manual_dpi_input)

            zoom_safe_enabled = prompt_zoom_safe_mode()

            # Now that inputs are validated, show the spinner and proceed.
            spinner.start()
            spinner.update(5, "Input validation")

            spinner.update(10, "PDF opened")

            spinner.update(20, "Page validated")

            if use_dynamic:
                spinner.update(30, "Preview analysis")
                preview = render_preview(pdf, page_index)
                complexity_score = analyze_page_complexity(preview)
                page = pdf[page_index]
                try:
                    page_width_pt, page_height_pt = page.get_size()
                finally:
                    page.close()
                selected_dpi = select_dynamic_dpi_for_page(
                    complexity_score,
                    page_width_pt,
                    page_height_pt,
                )
                del preview

            spinner.update(35, "DPI selected")

            spinner.update(45, "Zoom-safe DPI check")
            page_long_edge_px = estimate_page_long_edge_pixels(pdf, page_index, selected_dpi)
            effective_dpi = compute_effective_dpi(
                selected_dpi,
                zoom_safe_enabled,
                page_long_edge_px,
                DYNAMIC_DPI_MAX if use_dynamic else ZOOM_SAFE_MAX_DPI,
            )

            spinner.update(55, "High-resolution rendering")
            final_image = render_page(pdf, page_index, effective_dpi)

            spinner.update(70, "Pillow processing")
            optimized_image = optimize_image(final_image)
            final_image.close()

            spinner.update(90, "Lossless PNG optimization")
            dpi_mode_label = "Dynamic" if use_dynamic else "Manual"
            output_filename = build_output_filename(
                pdf_path,
                page_number,
                effective_dpi,
                dpi_mode_label,
            )
            output_path = output_folder / output_filename
            save_image(optimized_image, output_path, effective_dpi)
            optimized_image.close()

            spinner.update(97, "Output verification")
            width, height, file_size = verify_output_file(output_path, effective_dpi)

            spinner.update(100, "Completed")
            spinner.stop(success=True)

        finally:
            if pdf is not None:
                pdf.close()

        print()
        print("Success!")
        print()
        print("Image saved to:")
        print(str(output_path))
        print()
        print(f"Selected DPI: {selected_dpi}")
        print(f"Rendered DPI: {effective_dpi}")
        print(f"Image dimensions: {width} x {height} pixels")
        if zoom_safe_enabled and effective_dpi > selected_dpi:
            print(
                "Zoom-safe mode increased DPI to reduce visible pixel breakup at deep zoom."
            )
        print(
            "Note: Higher DPI creates more pixels, so at the same viewer zoom "
            "percentage it will appear more zoomed-in and sharper."
        )
        print(f"File size: {format_file_size(file_size)}")
        return 0

    except ValidationError as exc:
        spinner.stop(success=False)
        print()
        print(f"Error: {exc}")
        return 1

    except KeyboardInterrupt:
        spinner.stop(success=False)
        print()
        print("Operation cancelled by user.")
        return 1

    except MemoryError:
        spinner.stop(success=False)
        print()
        print("Error: Out of memory while processing this page. Try a lower DPI.")
        return 1

    except OSError as exc:
        spinner.stop(success=False)
        print()
        print(f"Error: File access problem - {exc}")
        return 1

    except Exception as exc:  # noqa: BLE001 - final safety net for unexpected errors
        spinner.stop(success=False)
        print()
        print(f"Error: An unexpected error occurred - {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
