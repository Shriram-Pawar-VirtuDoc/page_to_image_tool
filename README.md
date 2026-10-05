# Image Tool

Turns pages of the atlas / textbook / exam PDFs in `sources/` into small, sharp
**PNG** images for the VirtuDoc website.

## Setup (once)

```powershell
cd "C:\Users\Shriram Pawar\Desktop\page_to_image_tool"
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Use

Interactive — run it and answer the prompts (you can drag a PDF into the window):

```powershell
python image_tool.py
```

Command line:

```powershell
# one page, web preset (default)
python image_tool.py "sources\atlases\Anatomy\Thieme_Atlas_MSK_3e.pdf" -p 301

# several pages + phone-sized versions for srcset
python image_tool.py "...\Thieme_Atlas_MSK_3e.pdf" -p 10-20,35 --responsive

# higher-detail copy for a zoom viewer
python image_tool.py "...\Thieme_Atlas_MSK_3e.pdf" -p 301 --preset zoom
```

Page numbers are the PDF page position (1 = first page in the file), not the number
printed on the page.

## Presets

| Preset | Long edge | Size budget | Use for |
|---|---|---|---|
| `web` (default) | ≤ 2000 px | ≤ 500 KB | Normal page view on the website |
| `zoom` | ≤ 3200 px | ≤ 1.5 MB | Pinch / deep-zoom viewer |
| `thumb` | ≤ 600 px | ≤ 80 KB | Lists, search results, previews |

Override with `--max-kb`, `--max-edge`, `--dpi`. Other options: `--colors N` (fixed palette
size, 16–256), `--lossless` (exact full-colour PNG, much larger), `--trim` (crop white margins),
`--sharpen` (very mild), `-o FOLDER`, `--password`. Run `python image_tool.py --help` for everything.

## How it keeps PNGs small

1. Reads the resolution of the pictures embedded in the page (the Thieme atlases are ~150 ppi)
   and renders at that density. Rendering higher only upscales the picture: more bytes, no detail.
   Pages that are mostly text or charts render at 200 DPI.
2. Pages with few colours (charts, forms, text) are saved lossless.
3. Illustrated pages are saved as a 256-colour palette PNG with dithering (the same idea as
   pngquant). This looks almost identical to full colour and is about 60% smaller. If the page
   is still over budget, the tool tries 192 and 128 colours, then undithered versions, then scales
   the image down a little, but never below 1400 px (web preset), so labels stay readable.
   Photo-heavy pages such as histology photomicrographs may end up a bit over budget (~600 KB);
   the tool says so, and `--max-edge 1200` or `--colors 64` makes them smaller.
4. Grayscale pages are saved as grayscale PNG.

## Output

Files go to `Images_of_image_tool/` with URL-safe names, e.g.
`thieme-atlas-msk-3e_p301.png`. Running the same page again replaces the file.

`manifest.json` in the same folder lists every image's width, height, size and source page,
so the website can set `width`/`height` and avoid layout shift. After a single page the tool
also prints a ready-to-paste `<img>` tag.
