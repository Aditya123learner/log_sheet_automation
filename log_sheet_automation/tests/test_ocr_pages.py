# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""python -m unittest log_sheet_automation.tests.test_ocr_pages"""

import io
import unittest

from PIL import Image

from log_sheet_automation.integrations import ocr_pages


def scan(width, height, colour="white"):
	return Image.new("RGB", (width, height), colour)


def as_bytes(image, fmt):
	buffer = io.BytesIO()
	image.save(buffer, format=fmt)
	return buffer.getvalue()


def scanned_pdf(sizes):
	pages = [scan(w, h) for w, h in sizes]
	buffer = io.BytesIO()
	pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
	return buffer.getvalue()


class TestExtractPages(unittest.TestCase):
	def test_photo_is_one_page_and_is_shrunk(self):
		pages = ocr_pages.extract_pages(as_bytes(scan(6000, 4000), "JPEG"), "sheet.jpg")
		self.assertEqual(len(pages), 1)
		page = pages[0]
		self.assertEqual((page["page_no"], page["kind"]), (1, "image"))
		self.assertEqual(max(page["width"], page["height"]), ocr_pages.MAX_SIDE_PX)
		self.assertEqual(Image.open(io.BytesIO(page["content"])).format, "JPEG")

	def test_small_png_is_not_enlarged(self):
		page = ocr_pages.extract_pages(as_bytes(scan(900, 700), "PNG"), "sheet.png")[0]
		self.assertEqual((page["width"], page["height"]), (900, 700))

	def test_scanned_pdf_gives_one_image_per_page(self):
		pdf = scanned_pdf([(1200, 1600), (1000, 1400), (1100, 1500)])
		pages = ocr_pages.extract_pages(pdf, "Crane_Log_Sheets.pdf")
		self.assertEqual([(p["page_no"], p["kind"]) for p in pages], [(1, "image"), (2, "image"), (3, "image")])
		self.assertEqual((pages[1]["width"], pages[1]["height"]), (1000, 1400))

	def test_pdf_is_recognised_without_a_pdf_file_name(self):
		self.assertEqual(len(ocr_pages.extract_pages(scanned_pdf([(800, 1000)]), "upload")), 1)

	def test_every_page_fits_vision_request_limit(self):
		pdf = scanned_pdf([(3000, 4000)] * 2)
		for page in ocr_pages.extract_pages(pdf, "big.pdf"):
			self.assertLess(len(page["content"]) * 4 / 3, 10 * 1024 * 1024)

	def test_too_many_pages_is_refused(self):
		pdf = scanned_pdf([(600, 800)] * (ocr_pages.MAX_PAGES + 1))
		with self.assertRaises(ocr_pages.PageExtractionError):
			ocr_pages.extract_pages(pdf, "huge.pdf")

	def test_unreadable_file_is_refused_with_a_clear_message(self):
		with self.assertRaises(ocr_pages.PageExtractionError):
			ocr_pages.extract_pages(b"this is not an image", "notes.txt")
		with self.assertRaises(ocr_pages.PageExtractionError):
			ocr_pages.extract_pages(b"", "empty.pdf")


if __name__ == "__main__":
	unittest.main()
