#!/usr/bin/env python3
"""Render slides.html to a PDF, one slide per page.

Uses Playwright's Chromium so the PDF is produced by the same engine that
renders the deck in a browser, rather than a second-guessing HTML-to-PDF
converter. The deck's own `@media print` block forces every slide visible and
adds a page break after each one; without it Chromium would print only the
active slide, since the others are display:none.

    python3 tools/slides_to_pdf.py            # writes slides.pdf
    python3 tools/slides_to_pdf.py out.pdf
"""
import asyncio, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "slides.html"
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "slides.pdf"


async def main():
    from playwright.async_api import async_playwright

    if not SRC.exists():
        sys.exit(f"not found: {SRC}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 810})
        await page.goto(SRC.as_uri(), wait_until="networkidle")
        # Fragment Mono is webfont-loaded; printing before it lands would fall
        # back to a system mono and change every measurement on the page.
        await page.evaluate("document.fonts.ready")
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(OUT),
            width="1440px",
            height="810px",          # 16:9, matches how the deck is presented
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        await browser.close()

    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({kb:.0f} KB)")


if __name__ == "__main__":
    asyncio.run(main())
