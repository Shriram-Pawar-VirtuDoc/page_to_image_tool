# Image Tool

Renders one page of a PDF from `pdfs/` into a PNG for the VirtuDoc website.

## Setup (once)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Use

```text
python image_tool.py

PDF file path: <drag the PDF here>
Page number: 450

1) Dynamic DPI
2) Select DPI
Option: 1
```

The page number is the PDF page position (1 = first page in the file), the same as
"PDF p." in the image plans, not the number printed on the page.

The PNG is saved in `Images_of_image_tool/`, e.g. `thieme-atlas-muskuloskeletal-3rd-ed_p450_dynamic.png`
(`_600dpi.png` etc. for option 2). Running the same page and option again replaces the file.

## Options

- **Dynamic DPI (use this by default)**: renders at **450 DPI or higher**, as high as possible
  (up to 900 DPI) while the PNG stays **under 4 MB**. It renders at 450 first, predicts the
  highest DPI that will still fit, and steps down by 25 until one does. A higher DPI is only
  used if the colour quality is at least as good as at 450.
- **Select DPI**: renders at exactly the DPI you choose. There is no size limit, so 900–1200 DPI
  files can be large.

Thieme atlas drawings are stored at about 150 ppi inside the PDF. Going above 450 DPI makes
labels, lines and text sharper, but cannot add detail to the drawings themselves.

## Typical results (Dynamic DPI, under 4 MB)

Measured on 2026-10-08 (4–6 seconds per page):

| Page | Result |
|---|---|
| Thieme MSK p. 255 (dense) | 4725 × 5775 px, 3.59 MB, 525 DPI |
| Thieme MSK p. 333 | 5175 × 6325 px, 3.76 MB, 575 DPI |
| Thieme MSK p. 487 | 5625 × 6875 px, 3.72 MB, 625 DPI |
| Thieme MSK p. 450 | 6075 × 7425 px, 3.69 MB, 675 DPI |
| Human Sectional Anatomy p. 239 | 4873 × 6443 px, 3.51 MB, 500 DPI |

The exact numbers are printed after each run.

PNGs are kept small with an adaptive 256-colour palette and dithering, which looks almost the
same as full colour. Charts, text pages and grayscale pages are saved lossless.
