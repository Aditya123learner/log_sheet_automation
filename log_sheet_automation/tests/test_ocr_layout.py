# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Tests for the pure-Python OCR layout logic (no Frappe site needed):

    python -m unittest log_sheet_automation.tests.test_ocr_layout

The two page builders below reproduce the REAL printed layout of Sanghvi
Movers' Monthly and Weekly templates — the heading/label coordinates were
measured off the sample scans (a Monthly page at
2065x2800 px and a Weekly page at 1143x955 px) —
and add handwriting tokens (with made-up names) the way Google Vision returns them
(split at punctuation, digits misread as letters, AM/PM on a second line).
"""

import math
import unittest

from log_sheet_automation.integrations import ocr_layout as L


def annotation(tokens, skew_degrees=0.0, normalised=False, size=(2065, 2800)):
	"""tokens: [(text, x, y, w, h)] with x,y = top-left. Builds one Vision
	`responses[]` entry, optionally rotating the whole page to mimic a
	crooked scan."""
	angle = math.radians(skew_degrees)
	cos_a, sin_a = math.cos(angle), math.sin(angle)

	def turn(x, y):
		return x * cos_a - y * sin_a, x * sin_a + y * cos_a

	words = []
	for text, x, y, w, h in tokens:
		corners = [turn(x, y), turn(x + w, y), turn(x + w, y + h), turn(x, y + h)]
		if normalised:
			box = {"normalizedVertices": [{"x": cx / size[0], "y": cy / size[1]} for cx, cy in corners]}
		else:
			box = {"vertices": [{"x": round(cx), "y": round(cy)} for cx, cy in corners]}
		words.append({"symbols": [{"text": ch} for ch in text], "boundingBox": box, "confidence": 0.9})
	page = {"width": size[0], "height": size[1], "blocks": [{"paragraphs": [{"words": words}]}]}
	return {"fullTextAnnotation": {"pages": [page], "text": " ".join(t[0] for t in tokens)}}


MONTHLY_PRINTED = [
	("MONTHLY", 890, 468, 120, 21), ("CRANE", 1016, 470, 78, 19), ("LOGSHEET", 1103, 469, 118, 20),
	("OPERATOR", 165, 493, 129, 20), (":", 300, 495, 6, 16), ("MONTH", 979, 505, 90, 20), (":", 1250, 507, 6, 16),
	("CRANE", 164, 527, 80, 20), ("MODEL", 253, 527, 86, 19), (":", 345, 529, 6, 16), ("SITE", 976, 537, 45, 20),
	("REGN", 164, 560, 62, 22), (".", 227, 574, 5, 5), ("NO", 234, 560, 34, 22), ("/", 270, 560, 10, 24),
	("SR", 295, 561, 29, 20), ("NO", 332, 562, 35, 20), (".", 368, 574, 5, 5), (":", 380, 563, 6, 16),
	("CLIENT", 975, 571, 79, 20), (":", 1250, 573, 6, 16),
	("DATE", 183, 618, 59, 18), ("WORKING", 343, 600, 122, 21), ("TIME", 473, 603, 58, 20),
	("TOTAL", 628, 608, 76, 19), ("NORMAL", 755, 609, 105, 19), ("OT", 938, 610, 32, 19),
	("SINGN", 1051, 611, 73, 19), ("SIGN", 1190, 613, 56, 19), ("OF", 1253, 612, 31, 19),
	("WORKING", 1447, 618, 124, 23), ("DESCRIPTION", 1579, 612, 159, 25),
	("FROM", 362, 635, 69, 21), ("TO", 505, 640, 32, 20), ("HOURS", 630, 642, 81, 20), ("SHIFT", 775, 643, 65, 19),
	("HOURS", 914, 643, 82, 20), ("OF", 1073, 644, 30, 20), ("USER", 1211, 647, 58, 20), ("CRANE", 1048, 675, 78, 21),
	("TOTAL", 313, 1907, 58, 15), ("NO", 376, 1906, 28, 15), ("OF", 415, 1906, 24, 15), ("WORKING", 443, 1906, 91, 15),
	("DAYS", 540, 1904, 50, 19), ("TOTAL", 1254, 1907, 50, 15), ("OF", 1348, 1908, 22, 15),
	("OVERTIME", 1375, 1908, 93, 15), ("HOURS", 1472, 1908, 61, 14),
	("CERTIFIED", 320, 1993, 100, 19), ("BY", 428, 1993, 27, 18), ("5/09/26", 250, 2030, 110, 40),
	("SITE", 129, 2068, 54, 22), ("ENGINEER", 190, 2068, 120, 22), ("SIGNATURE", 496, 2066, 142, 22),
	("NOTE", 119, 2262, 64, 21),
	("1)Daily", 116, 2334, 81, 26), ("logsheet", 205, 2331, 98, 26), ("Total", 1002, 2313, 54, 19),
	("OT", 1500, 2313, 30, 19), ("Hours", 1540, 2313, 60, 19),
	("4)Payment", 109, 2458, 127, 25), ("30", 584, 2443, 26, 19), ("days", 617, 2441, 51, 23),
	("from", 677, 2439, 51, 20), ("the", 737, 2437, 35, 20), ("date", 780, 2436, 48, 20), ("of", 836, 2434, 23, 20),
]


def monthly_page(rows, skew_degrees=0.0, **kwargs):
	"""rows: list of dicts with the handwriting tokens of each table row."""
	tokens = list(MONTHLY_PRINTED)
	tokens += [
		("Ravi", 470, 485, 110, 45), ("|", 585, 482, 8, 50), ("Kulkarni", 600, 484, 240, 50),
		("AUGUST", 1290, 492, 150, 34), ("-", 1445, 505, 14, 6), ("26", 1465, 492, 50, 34),
		("SAC1600S", 420, 520, 210, 36), ("Rampur", 1290, 528, 250, 38),
		("MH12AB1234", 440, 552, 330, 40),
	]
	for i, row in enumerate(rows):
		y = 700 + 35 * i
		for text, x, w in row:
			tokens.append((text, x, y + (3 if x > 1300 else 0), w, 30))
	return annotation(tokens, skew_degrees=skew_degrees, **kwargs)


def monthly_row(day, total="24", normal="24", description=("Crane", "Marching"), date_style="pipes"):
	if date_style == "pipes":
		date_tokens = [(f"{day:02d}", 138, 34), ("|", 175, 6), ("08", 184, 34), ("|", 221, 6), ("26", 230, 34)]
	elif date_style == "glued":
		date_tokens = [(f"{day:02d}108126", 138, 130)]
	else:
		date_tokens = [(f"{day:02d}/08/26", 138, 130)]
	return date_tokens + [
		("8.00AM", 322, 118), ("8.00", 462, 66), ("AM", 530, 44),
		(total, 632, 50), (normal, 762, 50), ("-", 940, 22),
		("Ravi", 1040, 70), ("sgn", 1160, 60),
		(description[0], 1330, 110), (description[1], 1450, 150),
	]


WEEKLY_PRINTED = [
	("CLIENT", 965, 28, 60, 13), ("COPY", 1030, 28, 45, 13),
	("LOG", 14, 124, 31, 14), ("SHEET", 52, 126, 54, 15), ("NO", 111, 130, 22, 15), (".", 134, 140, 3, 4), (":", 139, 132, 4, 12),
	("WEEKLY", 430, 128, 90, 22), ("CRANE", 535, 128, 83, 20), ("LOG", 630, 128, 45, 19), ("SHEET", 687, 125, 80, 22),
	("MONTH", 840, 146, 65, 16), (":", 908, 148, 4, 12),
	("OPERATOR", 13, 169, 87, 17), (":", 180, 172, 4, 12),
	("W", 669, 196, 14, 13), (".", 684, 205, 3, 4), ("O", 695, 196, 12, 13), (".", 708, 205, 3, 4), ("NO", 716, 196, 20, 13), (".", 737, 205, 3, 4),
	("CRANE", 13, 214, 57, 15), ("MODEL", 76, 217, 58, 17), (":", 180, 220, 4, 12),
	("SITE", 671, 240, 36, 13), (":", 757, 242, 4, 12),
	("REGN", 12, 259, 44, 15), (".", 57, 270, 3, 4), ("NO", 69, 262, 20, 16), (".", 90, 272, 3, 4), ("/", 95, 262, 6, 16),
	("SR", 103, 262, 18, 16), (".", 122, 272, 3, 4), ("NO", 136, 265, 20, 14), (".", 157, 274, 3, 4), (":", 180, 266, 4, 12),
	("CLIENT", 671, 284, 55, 13), (":", 757, 286, 4, 12),
	("Day", 16, 318, 28, 12), ("Date", 140, 317, 32, 12),
	("Working", 208, 305, 55, 12), ("Time", 266, 305, 34, 12), ("(", 303, 305, 4, 13), ("am", 308, 307, 20, 9),
	("/", 329, 305, 5, 13), ("pm", 335, 307, 20, 11), (")", 356, 305, 4, 13),
	("From", 222, 330, 34, 11), ("To", 312, 330, 16, 11),
	("Total", 373, 324, 36, 12), ("Hours", 412, 324, 42, 12),
	("Normal", 478, 319, 46, 11), ("Shift", 486, 332, 30, 12), ("Hours", 482, 348, 40, 11),
	("Over", 553, 319, 30, 11), ("-", 584, 324, 5, 3), ("Time", 560, 334, 30, 11), ("Hours", 551, 348, 41, 11),
	("Break", 612, 319, 40, 11), ("Down", 614, 333, 36, 12), ("Hours", 612, 348, 40, 11),
	("Sign", 679, 324, 28, 12), ("of", 713, 324, 12, 11), ("Crane", 730, 324, 40, 11), ("Operator", 700, 339, 56, 14),
	("Sign", 795, 322, 28, 12), ("of", 826, 322, 13, 11), ("User", 843, 322, 30, 11), ("/", 874, 322, 5, 12), ("Client", 880, 322, 40, 11),
	("Site", 810, 337, 24, 11), ("In", 837, 337, 12, 11), ("-", 850, 342, 4, 3), ("Charge", 856, 337, 44, 13),
	("Work", 937, 322, 36, 12), ("Descrption", 977, 322, 78, 14),
	("MONDAY", 16, 362, 64, 14), ("TUESDAY", 13, 407, 70, 14), ("WEDNESDAY", 14, 452, 97, 15), ("THURSDAY", 13, 498, 82, 14),
	("FRIDAY", 14, 543, 60, 14), ("SATURDAY", 15, 589, 79, 13), ("SUNDAY", 14, 634, 62, 14),
	("TOTAL", 22, 684, 40, 13), ("NO", 66, 684, 16, 13), ("OF", 88, 684, 14, 13), ("WORKING", 106, 685, 60, 14),
	("DAYS", 170, 685, 30, 13), ("/", 202, 685, 5, 13), ("SHIFTS", 209, 685, 30, 13), ("=", 241, 690, 6, 4), ("6", 252, 678, 14, 22), ("Six", 420, 676, 40, 24),
	("TOTAL", 546, 686, 36, 12), ("NO", 586, 686, 14, 12), ("OF", 604, 686, 12, 12), ("OVERTIME", 620, 686, 60, 12),
	("HOURS", 684, 686, 38, 12), ("25", 730, 676, 26, 22),
	("HOUR", 18, 720, 34, 12), ("METER", 56, 720, 40, 12), ("READING", 100, 720, 52, 12), (":", 155, 720, 3, 12),
	("OPENING", 162, 720, 52, 12), ("=", 218, 724, 8, 5), ("2", 232, 708, 14, 24), ("o756", 248, 708, 62, 24),
	("CLOSING", 380, 720, 50, 12), ("20804", 436, 706, 90, 26),
	("TOTAL", 560, 722, 36, 12), ("NO", 600, 722, 14, 12), ("BREAKDOWN", 620, 722, 70, 12), ("HOURS", 694, 722, 38, 12),
	("CERTIFIED", 22, 756, 128, 20), ("BY", 160, 756, 30, 20),
	("4.", 11, 898, 10, 11), ("Payment", 26, 898, 54, 14), ("30", 268, 898, 13, 10), ("days", 287, 898, 27, 13),
	("from", 319, 898, 29, 10), ("the", 353, 897, 19, 10), ("date", 377, 897, 26, 10), ("OT", 777, 851, 15, 10),
	("Hours", 797, 851, 37, 11), ("Total", 527, 847, 29, 10), ("Day", 725, 850, 21, 13),
]

WEEKLY_HANDWRITING = [
	("5012", 155, 142, 66, 22),
	("September", 925, 124, 130, 26), ("-", 1058, 136, 8, 4), ("26", 1068, 126, 32, 22),
	("Anil", 240, 182, 70, 26), ("Sharma", 318, 182, 80, 26),
	("CKE", 236, 226, 60, 26), ("2500-2", 300, 226, 100, 26),
	("KA", 262, 272, 36, 24), ("05", 304, 272, 34, 24), ("1234", 346, 272, 66, 24),
	("ACL", 782, 232, 62, 24), ("Rampur", 850, 232, 110, 26),
	("Acme", 782, 276, 100, 26), ("Cement", 888, 276, 80, 24), ("M.P", 974, 276, 46, 24),
	# TUESDAY: 1/9/26, 9.00 AM -> 11.00 PM, total 14, OT 2
	("1/9/26", 119, 400, 65, 22), ("9.00", 207, 398, 46, 16), ("AM", 217, 418, 28, 16),
	("11.00", 290, 396, 50, 16), ("PM", 300, 418, 28, 16), ("14", 396, 404, 28, 22), ("2", 566, 404, 14, 22),
	("Sgn", 690, 400, 70, 24), ("ok", 820, 400, 30, 24), ("Cement", 946, 404, 59, 23), ("Silo", 1012, 404, 40, 23),
	# WEDNESDAY: date written with pipes and read as one glued number, 9:00AM -> 1.00 AM, 16, OT 4
	("219126", 112, 447, 72, 24), ("9:00AM", 204, 452, 70, 20), ("1.00", 292, 444, 40, 16), ("AM", 300, 464, 28, 16),
	("l6", 396, 450, 28, 22), ("4", 566, 450, 14, 22), ("Sgn", 690, 448, 70, 24),
	# THURSDAY: 3/9/26, 9.00AM -> 12.00 AM (midnight), total 15, OT 3
	("3/9/26", 119, 492, 65, 22), ("9.00AM", 204, 498, 70, 20), ("12.00", 290, 490, 46, 16), ("AM", 300, 510, 28, 16),
	("15", 396, 496, 28, 22), ("3", 566, 496, 14, 22),
	# FRIDAY: 4/9/26, 9:00AM -> 1:00 AM, total 16, OT 4
	("4/9/26", 119, 537, 65, 22), ("9:00AM", 204, 543, 70, 20), ("1:00", 292, 535, 40, 16), ("AM", 300, 555, 28, 16),
	("16", 396, 541, 28, 22), ("4", 566, 541, 14, 22),
	# SATURDAY: date badly misread, 9.00AM -> 2 AM, total 17, OT 5
	("S/g/2b", 119, 583, 65, 22), ("9.00AM", 204, 589, 70, 20), ("2.00", 292, 581, 40, 16), ("AM", 300, 601, 28, 16),
	("17", 396, 587, 28, 22), ("5", 566, 587, 14, 22), ("Diesel", 930, 580, 60, 22), ("121.85", 994, 580, 60, 22),
	# SUNDAY: 6/9/26, 7.00AM -> 2.00 PM, total 7, OT 7
	("6/9/26", 119, 628, 65, 22), ("7.00AM", 204, 634, 70, 20), ("2.00", 292, 626, 40, 16), ("PM", 300, 646, 28, 16),
	("7", 402, 632, 16, 22), ("7", 566, 632, 14, 22),
]


def weekly_page(skew_degrees=0.0, handwriting=None):
	return annotation(WEEKLY_PRINTED + (handwriting if handwriting is not None else WEEKLY_HANDWRITING),
		skew_degrees=skew_degrees, size=(1143, 955))


def parse(page):
	words, _text = L.vision_words(page)
	return L.parse_sheet(words)


class TestCellParsers(unittest.TestCase):
	def test_dates(self):
		self.assertEqual(L.parse_date_parts("14|08|26"), (14, 8, 2026))
		self.assertEqual(L.parse_date_parts("14108126"), (14, 8, 2026))
		self.assertEqual(L.parse_date_parts("1/9/26"), (1, 9, 2026))
		self.assertEqual(L.parse_date_parts("119126"), (1, 9, 2026))
		self.assertEqual(L.parse_date_parts("22/9/2026"), (22, 9, 2026))
		self.assertEqual(L.parse_date_parts("05-10-26"), (5, 10, 2026))
		self.assertEqual(L.parse_date_parts("31.08.26"), (31, 8, 2026))
		self.assertEqual(L.parse_date_parts("17"), (17, None, None))
		self.assertEqual(L.parse_date_parts("ab"), (None, None, None))
		self.assertEqual(L.parse_date_parts("45/13/26")[0], None)

	def test_times(self):
		self.assertEqual(L.parse_time("8.00AM"), "08:00:00")
		self.assertEqual(L.parse_time("8.OOAM"), "08:00:00")
		self.assertEqual(L.parse_time("9:00 am"), "09:00:00")
		self.assertEqual(L.parse_time("11.00PM"), "23:00:00")
		self.assertEqual(L.parse_time("12.00 AM"), "00:00:00")
		self.assertEqual(L.parse_time("12:30PM"), "12:30:00")
		self.assertEqual(L.parse_time("800AM"), "08:00:00")
		self.assertEqual(L.parse_time("2 PM"), "14:00:00")
		self.assertEqual(L.parse_time("14:30"), "14:30:00")
		self.assertIsNone(L.parse_time("AM"))
		self.assertIsNone(L.parse_time("99.00"))

	def test_durations(self):
		self.assertEqual(L.duration_hours("08:00:00", "08:00:00"), 24)
		self.assertEqual(L.duration_hours("09:00:00", "23:00:00"), 14)
		self.assertEqual(L.duration_hours("09:00:00", "01:00:00"), 16)
		self.assertIsNone(L.duration_hours(None, "01:00:00"))

	def test_hours(self):
		self.assertEqual(L.parse_hours("24"), (24.0, None))
		self.assertEqual(L.parse_hours("7.5"), (7.5, None))
		self.assertEqual(L.parse_hours("-"), (0.0, None))
		self.assertEqual(L.parse_hours(""), (0.0, None))
		value, note = L.parse_hours("2h")
		self.assertEqual(value, 24.0)
		self.assertIn("read '2h' as 24", note)
		self.assertEqual(L.parse_hours("l6")[0], 16.0)
		self.assertIsNone(L.parse_hours("241")[0])
		self.assertIsNone(L.parse_hours("xx")[0])


class TestMonthlySheet(unittest.TestCase):
	def rows(self):
		rows = [monthly_row(day) for day in range(14, 32)]
		rows[1] = monthly_row(15, total="2h", normal="24", description=("Derrick", "assembly"), date_style="slashes")
		rows[2] = monthly_row(16, date_style="glued", description=("Main", "boom"))
		return rows

	def check(self, sheet):
		self.assertEqual(sheet["template"], "Monthly")
		self.assertEqual(len(sheet["rows"]), 18, sheet["warnings"])
		self.assertEqual([r["log_date"] for r in sheet["rows"]], [f"2026-08-{d:02d}" for d in range(14, 32)])
		first = sheet["rows"][0]
		self.assertEqual((first["from_time"], first["to_time"]), ("08:00:00", "08:00:00"))
		self.assertEqual((first["total_hours"], first["normal_shift_hours"], first["overtime_hours"]), (24.0, 24.0, 0.0))
		self.assertEqual(first["work_description"], "Crane Marching")
		self.assertEqual(first["day_label"], "Friday")  # 14 Aug 2026
		self.assertGreaterEqual(first["confidence"], 0.75)
		# "2h" was accepted as 24 but must be flagged for review
		self.assertEqual(sheet["rows"][1]["total_hours"], 24.0)
		self.assertLess(sheet["rows"][1]["confidence"], 0.75)
		self.assertEqual((sheet["period_start"], sheet["period_end"], sheet["month"]), ("2026-08-14", "2026-08-31", "August 2026"))

	def test_straight_scan(self):
		sheet = parse(monthly_page(self.rows()))
		self.check(sheet)
		header = sheet["header"]
		self.assertIn("Kulkarni", header["operator"])
		self.assertEqual(header["crane_model"], "SAC1600S")
		self.assertEqual(header["regn_no"], "MH12AB1234")
		self.assertEqual(header["month_text"], "AUGUST-26")
		self.assertEqual(header["site"], "Rampur")
		self.assertNotIn("client", header)

	def test_crooked_scan(self):
		for degrees in (-2.0, 1.5, 3.0):
			with self.subTest(skew=degrees):
				self.check(parse(monthly_page(self.rows(), skew_degrees=degrees)))

	def test_page_photographed_sideways(self):
		self.check(parse(monthly_page(self.rows(), skew_degrees=90.0)))

	def test_pdf_style_normalised_coordinates(self):
		self.check(parse(monthly_page(self.rows(), normalised=True)))

	def test_signature_dates_below_table_are_not_rows(self):
		sheet = parse(monthly_page([monthly_row(day) for day in range(14, 20)]))
		self.assertEqual(len(sheet["rows"]), 6)

	def test_one_misread_date_is_repaired_from_neighbours(self):
		rows = [monthly_row(day) for day in range(14, 20)]
		rows[2] = [("lb", 138, 34), ("|", 175, 6), ("08", 184, 34), ("|", 221, 6), ("26", 230, 34)] + monthly_row(16)[5:]
		sheet = parse(monthly_page(rows))
		self.assertEqual(sheet["rows"][2]["log_date"], "2026-08-16")
		self.assertLess(sheet["rows"][2]["confidence"], 0.75)
		self.assertTrue(any("day sequence" in note for note in sheet["rows"][2]["notes"]))


class TestWeeklySheet(unittest.TestCase):
	def check(self, sheet):
		self.assertEqual(sheet["template"], "Weekly")
		rows = sheet["rows"]
		self.assertEqual([r["day_label"] for r in rows], ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], sheet["warnings"])
		self.assertEqual([r["log_date"] for r in rows], [f"2026-09-0{d}" for d in range(1, 7)])
		self.assertEqual([r["total_hours"] for r in rows], [14.0, 16.0, 15.0, 16.0, 17.0, 7.0])
		self.assertEqual([r["overtime_hours"] for r in rows], [2.0, 4.0, 3.0, 4.0, 5.0, 7.0])
		self.assertEqual((rows[0]["from_time"], rows[0]["to_time"]), ("09:00:00", "23:00:00"))
		self.assertEqual((rows[1]["from_time"], rows[1]["to_time"]), ("09:00:00", "01:00:00"))
		self.assertEqual((rows[5]["from_time"], rows[5]["to_time"]), ("07:00:00", "14:00:00"))
		self.assertEqual(rows[0]["work_description"], "Cement Silo")
		self.assertGreaterEqual(rows[0]["confidence"], 0.75)   # clean row
		self.assertLess(rows[4]["confidence"], 0.75)           # Saturday's date was unreadable -> inferred
		self.assertEqual((sheet["period_start"], sheet["period_end"], sheet["month"]), ("2026-09-01", "2026-09-06", "September 2026"))

	def test_straight_scan(self):
		sheet = parse(weekly_page())
		self.check(sheet)
		header = sheet["header"]
		self.assertEqual(header["log_sheet_no"], "5012")
		self.assertEqual(header["operator"], "Anil Sharma")
		self.assertEqual(header["crane_model"], "CKE 2500-2")
		self.assertEqual(header["regn_no"], "KA051234")
		self.assertEqual(header["month_text"], "September-26")
		self.assertEqual(header["site"], "ACL Rampur")
		self.assertEqual(header["client"], "Acme Cement M.P")
		self.assertEqual(header["hour_meter_opening"], 20756.0)
		self.assertEqual(header["hour_meter_closing"], 20804.0)

	def test_crooked_scan(self):
		for degrees in (-1.5, 2.5):
			with self.subTest(skew=degrees):
				self.check(parse(weekly_page(skew_degrees=degrees)))

	def test_missed_day_names_are_interpolated(self):
		printed = [t for t in WEEKLY_PRINTED if t[0] not in ("FRIDAY", "WEDNESDAY")]
		sheet = L.parse_sheet(L.vision_words(annotation(printed + WEEKLY_HANDWRITING, size=(1143, 955)))[0])
		self.check(sheet)

	def test_struck_through_sheet_keeps_only_the_worked_day(self):
		handwriting = [
			("21/9/26", 119, 356, 65, 22), ("9.00", 207, 352, 46, 16), ("AM", 217, 372, 28, 16),
			("1.00", 292, 352, 40, 16), ("AM", 300, 372, 28, 16), ("16", 396, 358, 28, 22), ("4", 566, 358, 14, 22),
			("22/9/26", 119, 401, 65, 22),
		]
		sheet = parse(weekly_page(handwriting=handwriting))
		self.assertEqual([(r["day_label"], r["log_date"], r["total_hours"]) for r in sheet["rows"]], [("Monday", "2026-09-21", 16.0)])


class TestPatternsSeenOnLiveScans(unittest.TestCase):
	"""Each case below is a pattern Google Vision actually returned for the
	real sheets (word positions kept, names changed)."""

	def test_date_glued_to_from_time(self):
		expected = (9, 2026)
		cases = {
			"1419269.00AM": (14, 9, 2026, "9.00AM"),   # 14/9/26 + 9.00: one '/' read as 1, one dropped
			"15191269.00AM": (15, 9, 2026, "9.00AM"),  # both '/' read as 1
			"719126900AM": (7, 9, 2026, "900AM"),
			"1219/269.00AM": (12, 9, 2026, "9.00AM"),
			"18/91269.00Am": (18, 9, 2026, "9.00Am"),
			"10/9/26/1900AM": (10, 9, 2026, "/1900AM"),
			"119/260.00AM": (1, 9, 2026, "0.00AM"),
			"2219/26": (22, 9, 2026, ""),
			"2/9/269:00AM": (2, 9, 2026, "9:00AM"),
		}
		for text, want in cases.items():
			with self.subTest(text=text):
				best = L._best_date_candidate(L.date_time_candidates(text), expected)
				self.assertEqual(best[:4], want)
		self.assertEqual(L.date_time_candidates("9.00AM"), [])
		self.assertEqual(L.date_time_candidates("11.00PM"), [])

	def test_month_box(self):
		self.assertEqual(L.parse_month_text("September-26"), (9, 2026))
		self.assertEqual(L.parse_month_text("AUGUST-2026"), (8, 2026))
		self.assertEqual(L.parse_month_text("August"), (8, None))
		self.assertEqual(L.parse_month_text(None), (None, None))

	def test_times(self):
		self.assertIsNone(L.parse_time("0.00AM"))          # hour digit misread: no 0 o'clock with AM/PM
		self.assertIsNone(L.parse_time("00AM"))            # hour digit lost
		self.assertEqual(L.parse_time_ex("1.80AM")[0], "01:00:00")
		self.assertIn("not valid", L.parse_time_ex("1.80AM")[1])
		self.assertEqual(L.parse_time("1900AM"), "09:00:00")   # column line read as a leading 1
		self.assertEqual(L.parse_time("&.00AM"), "08:00:00")   # 8 read as &
		self.assertEqual(L.parse_time("1000PM"), "22:00:00")

	def test_hours(self):
		self.assertEqual(L.parse_hours("2424")[0], 24.0)   # Total and Normal cells read as one word
		self.assertEqual(L.parse_hours("A")[0], 4.0)
		self.assertIsNone(L.parse_hours("214")[0])

	def low_resolution_weekly(self, handwriting):
		# On the low-resolution pages Vision did not read the small "From" / "To" headings.
		printed = [t for t in WEEKLY_PRINTED if t[0] not in ("From", "To")]
		return parse(annotation(printed + handwriting, size=(1143, 955)))

	def test_weekly_page_with_glued_dates_and_no_from_to_headings(self):
		sheet = self.low_resolution_weekly([
			("September", 925, 124, 130, 26), ("2026", 1068, 126, 44, 22),
			("1419269.00", 120, 356, 140, 22), ("AM", 217, 374, 28, 16), ("1.00", 292, 352, 40, 16), ("AM", 300, 372, 28, 16),
			("16", 396, 358, 28, 22), ("4", 566, 358, 14, 22),
			("1519", 112, 401, 50, 22), ("/", 166, 401, 6, 22), ("269.00", 176, 399, 84, 22), ("AM", 217, 420, 28, 16),
			("12.00", 290, 397, 46, 16), ("AM", 300, 418, 28, 16), ("15", 396, 403, 28, 22), ("3", 566, 403, 14, 22),
			("16/9/26/1900", 112, 446, 150, 22), ("1.00", 292, 444, 40, 16), ("AM", 300, 464, 28, 16),
			("16", 396, 450, 28, 22), ("A", 566, 450, 14, 22),
		])
		rows = sheet["rows"]
		self.assertEqual([r["log_date"] for r in rows], ["2026-09-14", "2026-09-15", "2026-09-16"])
		self.assertEqual([r["from_time"] for r in rows], ["09:00:00"] * 3)
		self.assertEqual([r["to_time"] for r in rows], ["01:00:00", "00:00:00", "01:00:00"])
		self.assertEqual([r["total_hours"] for r in rows], [16.0, 15.0, 16.0])
		self.assertEqual([r["overtime_hours"] for r in rows], [4.0, 3.0, 4.0])
		self.assertGreaterEqual(rows[0]["confidence"], 0.75)    # every cross-check agrees
		self.assertLess(rows[2]["confidence"], 0.75)            # 'A' taken as 4, column line ignored

	def test_overtime_written_in_normal_shift_column(self):
		handwriting = [("September", 925, 124, 130, 26), ("2026", 1068, 126, 44, 22), ("19", 730, 676, 26, 22)]
		for i, (day, to, to_ampm, total, overtime) in enumerate([(7, "9.00", "PM", "12", None), (8, "10.00", "PM", "13", "1"), (9, "11.00", "PM", "14", "2"), (10, "1.00", "AM", "16", "4")]):
			y = 356 + 45 * i
			handwriting += [(f"{day}/9/26", 119, y, 65, 22), ("9.00", 207, y - 4, 46, 16), ("AM", 217, y + 16, 28, 16),
				(to, 292, y - 4, 40, 16), (to_ampm, 300, y + 16, 28, 16), (total, 396, y + 2, 28, 22)]
			if overtime:
				handwriting.append((overtime, 496, y + 2, 14, 22))   # under "Normal Shift Hours", not "Over-Time"
		sheet = parse(weekly_page(handwriting=handwriting))
		rows = sheet["rows"]
		self.assertEqual([r["overtime_hours"] for r in rows], [0.0, 1.0, 2.0, 4.0])
		self.assertEqual([r["normal_shift_hours"] for r in rows], [0.0] * 4)
		for row in rows[1:]:
			self.assertLess(row["confidence"], 0.75)
			self.assertTrue(any("Normal Shift column" in note for note in row["notes"]))
		# the sheet's own overtime total (19) does not match the rows (7): warned
		self.assertTrue(any("Total No. of Overtime Hours" in w for w in sheet["warnings"]))

	def test_sheet_totals_that_agree_raise_no_warning(self):
		sheet = parse(weekly_page())   # footer says 6 days / 25 overtime hours, as do the rows
		self.assertEqual(sheet["header"]["sheet_working_days"], 6.0)
		self.assertEqual(sheet["header"]["sheet_overtime_hours"], 25.0)
		self.assertFalse([w for w in sheet["warnings"] if "sheet's own" in w])

	def test_missed_overtime_entry_is_caught_by_the_sheet_total(self):
		handwriting = [t for t in WEEKLY_HANDWRITING if not (t[0] == "4" and t[2] == 541)]   # Friday's OT not read
		sheet = parse(weekly_page(handwriting=handwriting))
		self.assertTrue(any("is 25 but the rows read add up to 21" in w for w in sheet["warnings"]))

	def test_lookalike_letters_and_scribbles(self):
		handwriting = [t for t in WEEKLY_HANDWRITING if t[0] != "CKE"] + [("ске", 236, 226, 60, 26), ("سلم", 946, 452, 50, 20)]
		sheet = parse(weekly_page(handwriting=handwriting))
		self.assertEqual(sheet["header"]["crane_model"], "cke 2500-2")
		self.assertIsNone(sheet["rows"][1]["work_description"])

	def test_something_written_under_wo_no_is_not_the_log_sheet_number(self):
		sheet = parse(weekly_page(handwriting=WEEKLY_HANDWRITING + [("D.", 700, 150, 16, 14)]))
		self.assertEqual(sheet["header"]["log_sheet_no"], "5012")

	def test_blank_normal_shift_on_one_monthly_row_is_flagged(self):
		rows = [monthly_row(day) for day in range(14, 24)]
		rows[4] = [t for t in rows[4] if t[1] != 762]   # Normal Shift cell not read
		sheet = parse(monthly_page(rows))
		self.assertEqual(sheet["rows"][4]["normal_shift_hours"], 0.0)
		self.assertLess(sheet["rows"][4]["confidence"], 0.75)
		self.assertGreaterEqual(sheet["rows"][3]["confidence"], 0.75)


class TestUnreadablePages(unittest.TestCase):
	def test_blank_page(self):
		sheet = L.parse_sheet([])
		self.assertEqual(sheet["rows"], [])
		self.assertTrue(sheet["warnings"])

	def test_page_that_is_not_a_log_sheet(self):
		page = annotation([("Invoice", 100, 100, 200, 40), ("Total", 100, 300, 80, 30), ("4500", 300, 300, 80, 30)])
		sheet = parse(page)
		self.assertEqual(sheet["rows"], [])
		self.assertTrue(sheet["warnings"])


if __name__ == "__main__":
	unittest.main()
