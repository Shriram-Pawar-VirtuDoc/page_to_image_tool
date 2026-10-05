# Image Tool

Renders one page of a PDF from `pdfs/` into a compact PNG for the VirtuDoc website.

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
Option: 2
DPI (300 / 450 / 600 / 900 / 1200): 600
```

The page number is the PDF page position (1 = first page in the file), the same as
"PDF p." in the image plans, not the number printed on the page.

The PNG is saved in `Images_of_image_tool/`, e.g. `thieme-atlas-muskuloskeletal-3rd-ed_p450_600dpi.png`
(`_dynamic.png` for option 1). Running the same page and option again replaces the file.

## Options

- **Dynamic DPI**: picks the DPI from the resolution of the page's own pictures, up to 3200 px,
  and keeps the file around 1.5 MB or less.
- **Select DPI**: renders at exactly the DPI you choose. Thieme atlas pictures are stored at about
  150 ppi, so 900–1200 DPI mostly makes text and lines sharper and the file larger, not the drawings.

## Typical results (Thieme MSK, PDF p. 450)

| Option | Pixels | Size | Time |
|---|---|---|---|
| Dynamic | 2024 × 2474 | 576 KB | 1 s |
| 300 DPI | 2700 × 3301 | 0.9 MB | 2 s |
| 600 DPI | 5400 × 6601 | 2.9 MB | 3 s |
| 1200 DPI | 10800 × 13201 | 9.6 MB | 12 s (≈1.6 GB RAM) |

PNGs are kept small with an adaptive 256-colour palette and dithering, which looks almost the
same as full colour at about 60% less size. Charts, text pages and grayscale pages are saved lossless.
