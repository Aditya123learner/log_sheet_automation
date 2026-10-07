# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Turns whatever the operator attached (a phone photo, a scanner JPG/PNG, or
a multi-page scanned PDF) into a list of page images Google Vision will
accept.

Why this exists: Vision's `images:annotate` endpoint only takes images (not
PDFs) and rejects requests above roughly 10 MB — but the real log sheets
arrive as multi-page scanner-app PDFs (one physical sheet per page), some of
them over 20 MB. So every page is pulled out as its own image and shrunk to
a size that is still easily readable by OCR.

This module deliberately has NO `frappe` import — it only needs `pypdf` and
`Pillow`, both of which ship with every Frappe bench — so it can be unit
tested on its own (see tests/test_ocr_pages.py).
"""

import io

# Longest side, in pixels, a page image is shrunk to before upload. A4 at
# ~250 dpi; handwriting on these sheets stays comfortably legible, and the
# JPEG lands around 0.5–1.5 MB (Vision's request limit is ~10 MB).
MAX_SIDE_PX = 2800
JPEG_QUALITY = 85
# Anything smaller than this on its longest side inside a PDF page is a
# logo/watermark (e.g. the "Scanned with ... Scanner" strip), not the scan.
MIN_SCAN_SIDE_PX = 500
# Hard stop so a wrongly attached 300-page PDF can't run up a Vision bill.
MAX_PAGES = 40


class PageExtractionError(Exception):
	"""Raised with a message that is safe to show to the operator."""


def is_pdf(content, file_name=None):
	if content[:1024].lstrip().startswith(b"%PDF"):
		return True
	return bool(file_name) and str(file_name).lower().endswith(".pdf")


def extract_pages(content, file_name=None):
	"""Returns a list of page dicts, in page order:

		{"page_no": 1, "kind": "image", "content": <JPEG bytes>, "width": w, "height": h}
		{"page_no": 2, "kind": "pdf",   "content": <single-page PDF bytes>}

	"image" pages go to Vision's images:annotate; "pdf" pages (only produced
	for a PDF page that has no embedded scan image, i.e. a computer-generated
	PDF) go to files:annotate instead.
	"""
	if not content:
		raise PageExtractionError("The attached Source Document is empty.")

	if is_pdf(content, file_name):
		return _extract_pdf_pages(content)
	return [_image_page(1, content)]


def _image_page(page_no, raw_bytes, rotation=0):
	try:
		from PIL import Image, ImageOps
	except ImportError as e:  # pragma: no cover - Pillow ships with Frappe
		raise PageExtractionError("The 'Pillow' Python package is required to prepare images for OCR.") from e

	try:
		image = Image.open(io.BytesIO(raw_bytes))
		image.load()
	except Exception as e:
		raise PageExtractionError(
			"The attached Source Document is not a readable image or PDF (supported: PDF, JPG, PNG, TIFF, WEBP, BMP)."
		) from e

	return _normalise_image(page_no, image, rotation, ImageOps)


def _normalise_image(page_no, image, rotation, ImageOps):
	# Phone cameras store "this photo is sideways" as EXIF metadata rather
	# than rotating the pixels; apply it so Vision sees the sheet upright.
	try:
		image = ImageOps.exif_transpose(image)
	except Exception:
		pass

	if rotation:
		# PDF /Rotate is clockwise; PIL rotates counter-clockwise.
		image = image.rotate(-rotation, expand=True)

	if image.mode not in ("RGB", "L"):
		image = image.convert("RGB")

	longest = max(image.size)
	if longest > MAX_SIDE_PX:
		scale = MAX_SIDE_PX / float(longest)
		image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))

	buffer = io.BytesIO()
	image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
	return {
		"page_no": page_no,
		"kind": "image",
		"content": buffer.getvalue(),
		"width": image.width,
		"height": image.height,
	}


def _extract_pdf_pages(content):
	try:
		from pypdf import PdfReader, PdfWriter
	except ImportError as e:  # pragma: no cover - pypdf ships with Frappe
		raise PageExtractionError("The 'pypdf' Python package is required to read PDF log sheets.") from e
	from PIL import ImageOps

	try:
		reader = PdfReader(io.BytesIO(content))
		if reader.is_encrypted:
			reader.decrypt("")
		page_count = len(reader.pages)
	except Exception as e:
		raise PageExtractionError("The attached PDF could not be opened — it may be damaged or password-protected.") from e

	if page_count == 0:
		raise PageExtractionError("The attached PDF has no pages.")
	if page_count > MAX_PAGES:
		raise PageExtractionError(
			f"The attached PDF has {page_count} pages; at most {MAX_PAGES} log sheets can be read in one run. "
			"Split it into smaller PDFs."
		)

	pages = []
	for index, page in enumerate(reader.pages, start=1):
		scan = _largest_embedded_image(page)
		if scan is not None:
			rotation = int(page.get("/Rotate") or 0) % 360
			pages.append(_normalise_image(index, scan, rotation, ImageOps))
			continue

		# No embedded scan: a "real" (vector/text) PDF page. Hand Vision that
		# one page as a PDF via files:annotate instead.
		writer = PdfWriter()
		writer.add_page(page)
		buffer = io.BytesIO()
		writer.write(buffer)
		pages.append({"page_no": index, "kind": "pdf", "content": buffer.getvalue()})
	return pages


def _largest_embedded_image(page):
	best = None
	best_area = 0
	try:
		images = list(page.images)
	except Exception:
		return None
	for embedded in images:
		try:
			image = embedded.image
			if image is None:
				continue
			image.load()
		except Exception:
			continue
		if max(image.size) < MIN_SCAN_SIDE_PX:
			continue
		area = image.width * image.height
		if area > best_area:
			best, best_area = image, area
	return best
