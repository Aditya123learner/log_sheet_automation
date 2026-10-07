# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Rebuilds a crane log sheet's table from Google Vision's word positions.

Vision only returns "these words, at these pixel positions" — it has no idea
the page is a table. The earlier version of this app tried to match one
regular expression against each *line* of Vision's plain text; on a ruled,
handwritten form Vision emits roughly one line per table CELL, so that
pattern never matched and OCR produced zero rows.

This module works from geometry instead:

1. `vision_words()` flattens Vision's response into a list of words, each
   with a bounding box and a confidence, and straightens a slightly rotated
   scan (deskew).
2. The printed column headings (DATE / FROM / TO / TOTAL HOURS / NORMAL
   SHIFT / OT HOURS / ...) are located — printed text is what OCR reads most
   reliably — and their x-positions define the table's columns.
3. Rows are located by the printed day names (Weekly sheet: MONDAY…SUNDAY)
   or by the clusters of handwriting in the DATE column (Monthly sheet).
4. Every word under the header is dropped into its (row, column) cell, and
   each cell is parsed as a date / time / number of hours with tolerance for
   the usual handwriting misreads ("2h" for 24, "|" for "/", "O" for 0).
5. Each row gets a confidence built from Vision's own word confidences and
   from cross-checks (do the hours agree with From–To? do Normal + OT add up
   to Total? do the dates run in sequence?). Anything doubtful is flagged
   for the operator in AI Review rather than silently accepted.

The printed header block (Operator, Crane Model, Regn. No., Month, Site,
Client, Log Sheet No., hour meter) is read the same way: find the printed
label, take the handwriting next to it.

No `frappe` import here on purpose — this is plain Python so it can be unit
tested against saved Vision word dumps (see tests/test_ocr_layout.py).
"""

import difflib
import math
import re
import statistics
from datetime import date, timedelta

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTH_NAMES = [
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
]

DEFAULT_WORD_CONFIDENCE = 0.75

# Handwritten digits that Vision commonly returns as letters. Only applied
# inside cells that are supposed to hold a number.
DIGIT_CONFUSIONS = str.maketrans({
	"h": "4", "H": "4", "u": "4", "U": "4", "y": "4", "A": "4",
	"l": "1", "I": "1", "|": "1", "!": "1", "i": "1",
	"O": "0", "o": "0", "D": "0", "Q": "0",
	"S": "5", "s": "5",
	"Z": "2", "z": "2",
	"b": "6", "G": "6",
	"g": "9", "q": "9",
	"B": "8",
})

# Vision sometimes returns Latin handwriting as look-alike Cyrillic/Greek
# letters ("ске" for "cke", "АР" for "AP"). Map them back so the text is
# searchable and matches masters.
LOOKALIKES = str.maketrans(
	"АВЕКМНОРСТХУаеосрхукмтнвΑΒΕΚΜΝΟΡΤΧ",
	"ABEKMHOPCTXYaeocpxykmthbABEKMNOPTX",
)

EMPTY_MARKS = set("-–—_.~=·'`,:;/\\|")

# Words that are part of the printed table heading. Used to keep the heading's
# lower lines ("HOURS", "SHIFT", "Operator", ...) out of the first data row.
HEADER_VOCAB = {
	"day", "date", "working", "time", "from", "to", "total", "hours", "normal", "shift", "over", "overtime",
	"ot", "break", "down", "breakdown", "sign", "singn", "of", "crane", "operator", "user", "client",
	"userclient", "site", "in", "charge", "incharge", "work", "description", "descrption",
}


# ---------------------------------------------------------------------------
# 1. Vision response -> flat, deskewed word list
# ---------------------------------------------------------------------------

def vision_words(annotation):
	"""`annotation` is ONE entry of Vision's `responses` list (i.e. one page).
	Returns (words, full_text). Each word is a dict: text, x0, y0, x1, y1,
	cx, cy, w, h, conf."""
	full = annotation.get("fullTextAnnotation") or {}
	return words_from_raw(raw_words(annotation)), (full.get("text") or "")


def raw_words(annotation):
	"""Vision's words exactly as returned (text, four corner points in page
	pixels, confidence) — before deskewing. This is what gets saved as the
	per-sheet "word dump" so the layout logic can be re-run and tuned later
	without calling Vision again."""
	full = annotation.get("fullTextAnnotation") or {}
	raw = []
	for page in full.get("pages") or []:
		page_w = page.get("width") or 1
		page_h = page.get("height") or 1
		for block in page.get("blocks") or []:
			for paragraph in block.get("paragraphs") or []:
				for word in paragraph.get("words") or []:
					text = "".join(symbol.get("text", "") for symbol in word.get("symbols") or [])
					if not text.strip():
						continue
					box = word.get("boundingBox") or {}
					if box.get("vertices"):
						points = [(float(v.get("x", 0)), float(v.get("y", 0))) for v in box["vertices"]]
					else:
						# files:annotate (PDF input) reports fractions of the page.
						points = [
							(float(v.get("x", 0)) * page_w, float(v.get("y", 0)) * page_h)
							for v in box.get("normalizedVertices") or []
						]
					if len(points) < 4:
						continue
					raw.append({"text": text, "points": points, "conf": word.get("confidence")})
	return raw


def words_from_raw(raw):
	"""raw: [{"text", "points": [(x, y) x4, clockwise from top-left], "conf"}].
	Also the entry point for re-running the layout logic on a saved word dump."""
	angle = _skew_angle(raw)
	cos_a, sin_a = math.cos(-angle), math.sin(-angle)
	words = []
	for item in raw:
		points = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in item["points"]]
		xs = [p[0] for p in points]
		ys = [p[1] for p in points]
		x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
		words.append({
			"text": item["text"].translate(LOOKALIKES),
			"x0": x0, "x1": x1, "y0": y0, "y1": y1,
			"cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
			"w": max(x1 - x0, 1.0), "h": max(y1 - y0, 1.0),
			"conf": item.get("conf"),
		})
	return words


def _skew_angle(raw):
	"""Median direction of the words' top edges, in radians. A scan that is a
	degree or two off-square otherwise smears rows into each other; this also
	rights a page photographed sideways."""
	angles = []
	for item in raw:
		if len(item["text"]) < 3:
			continue
		(xa, ya), (xb, yb) = item["points"][0], item["points"][1]
		if abs(xb - xa) + abs(yb - ya) < 4:
			continue
		angles.append(math.atan2(yb - ya, xb - xa))
	if len(angles) < 5:
		return 0.0
	angle = statistics.median(angles)
	return 0.0 if abs(angle) < math.radians(0.15) else angle


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------

def _norm(text):
	return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _is_foreign_script(text):
	"""True for a word in a script these sheets are never written in (Vision
	reads signature scribbles as Arabic, Devanagari, ...)."""
	return any(ord(ch) > 0x024F and ch.isalpha() for ch in text or "")


def _similar(a, b):
	return difflib.SequenceMatcher(None, a, b).ratio()


def _join(words, sep=""):
	return sep.join(w["text"] for w in sorted(words, key=lambda w: w["cx"]))


def _header_key(word):
	n = _norm(word["text"])
	if not n:
		return None
	if n == "date":
		return "date"
	if n == "from":
		return "from"
	if n in ("total", "totai", "tota1"):
		return "total"
	if n == "normal":
		return "normal"
	if n == "ot" or n.startswith("over"):
		return "ot"
	if n == "break" or n == "breakdown":
		return "break"
	if n in ("sign", "singn", "signof"):
		return "sign"
	if len(n) >= 8 and _similar(n, "description") >= 0.75:
		return "desc"
	return None


# ---------------------------------------------------------------------------
# 2. Locate the printed table header and derive the columns
# ---------------------------------------------------------------------------

def _find_header(words):
	keyed = [(key, w) for w in words for key in [_header_key(w)] if key]
	if not keyed:
		return None
	h_med = statistics.median(w["h"] for _, w in keyed)
	keyed.sort(key=lambda kw: kw[1]["cy"])

	clusters, current = [], [keyed[0]]
	for item in keyed[1:]:
		if item[1]["cy"] - current[-1][1]["cy"] > 2.5 * h_med:
			clusters.append(current)
			current = [item]
		else:
			current.append(item)
	clusters.append(current)

	def score(cluster):
		keys = {key for key, _ in cluster}
		if not ({"total", "normal", "from"} & keys):
			return 0
		return len(keys)

	best = max(clusters, key=lambda c: (score(c), -c[0][1]["cy"]))
	if score(best) < 3:
		return None

	# Reference line = the topmost of TOTAL / NORMAL / FROM (the first printed
	# line of the header in both real templates).
	refs = [w for key, w in best if key in ("total", "normal", "from")]
	ref = min(refs, key=lambda w: w["cy"])
	h = statistics.median(w["h"] for _, w in best)
	top, bottom = ref["cy"] - 2.5 * h, ref["cy"] + 2.2 * h
	band = [w for w in words if top <= w["cy"] <= bottom]
	# Every printed heading word, including the heading's 2nd/3rd lines that
	# hang below `bottom` ("HOURS", "SHIFT", "CRANE", "Operator", ...).
	printed = [
		w for w in words
		if top <= w["cy"] <= ref["cy"] + 4.5 * h and (_norm(w["text"]) in HEADER_VOCAB or _header_key(w))
	]
	heading_top = [w for w in printed if _header_key(w) or _norm(w["text"]) in ("working", "time", "day")]
	block_limit = min(w["y0"] for w in heading_top) - 0.2 * h
	return {
		"ref": ref, "h": h, "top": top, "bottom": bottom, "band": band,
		"printed_ids": {id(w) for w in printed}, "block_limit": block_limit,
	}


def _columns(header, template):
	band = header["band"]
	h = header["h"]

	def find(key):
		return sorted((w for w in band if _header_key(w) == key), key=lambda w: w["cx"])

	def phrase_centre(word, follower):
		"""Centre of 'word' or, if `follower` is printed right after it on the
		same line (e.g. 'Total Hours'), of the two words together."""
		for other in band:
			if _norm(other["text"]) == follower and abs(other["cy"] - word["cy"]) < 0.6 * h:
				if 0 <= other["x0"] - word["x1"] < 1.8 * h:
					return (word["x0"] + other["x1"]) / 2.0
		return word["cx"]

	totals, froms = find("total"), find("from")
	if not totals:
		return None, ["Could not find the printed 'TOTAL HOURS' column heading."]
	total_x = phrase_centre(totals[0], "hours")

	from_w = next((w for w in froms if w["cx"] < total_x), None)
	dates = [w for w in find("date") if w["cx"] < (from_w["cx"] if from_w else total_x)]
	date_w = dates[-1] if dates else None
	if not from_w and not date_w:
		return None, ["Could not find the printed 'DATE' / 'FROM' column headings."]

	warnings = []
	to_w = None
	if from_w:
		candidates = [
			w for w in band
			if _norm(w["text"]) == "to" and from_w["cx"] < w["cx"] < total_x and abs(w["cy"] - from_w["cy"]) < h
		]
		to_w = min(candidates, key=lambda w: w["cx"]) if candidates else None

	if from_w and to_w:
		from_x, to_x = from_w["cx"], to_w["cx"]
	elif from_w:
		from_x = from_w["cx"]
		to_x = (from_x + total_x) / 2.0
	else:
		from_x = date_w["cx"] + (total_x - date_w["cx"]) / 3.0
		to_x = date_w["cx"] + 2.0 * (total_x - date_w["cx"]) / 3.0
		warnings.append("Printed 'FROM' heading not found; time columns were estimated.")
	date_x = date_w["cx"] if date_w else from_x - (to_x - from_x)

	normals = [w for w in find("normal") if w["cx"] > total_x]
	normal_x = normals[0]["cx"] if normals else total_x + (total_x - to_x)
	ots = [w for w in find("ot") if w["cx"] > normal_x]
	ot_x = ots[0]["cx"] if ots else normal_x + (normal_x - total_x)

	columns = [("date", date_x), ("from", from_x), ("to", to_x), ("total", total_x), ("normal", normal_x), ("ot", ot_x)]

	last_x = ot_x
	breaks = [w for w in find("break") if w["cx"] > ot_x]
	if breaks:
		columns.append(("break", breaks[0]["cx"]))
		last_x = breaks[0]["cx"]
	elif template == "Weekly":
		last_x = ot_x + (ot_x - normal_x)
		columns.append(("break", last_x))

	signs = [w for w in find("sign") if w["cx"] > last_x]
	pitch = columns[-1][1] - columns[-2][1]
	sign1_x = signs[0]["cx"] if signs else last_x + pitch
	sign2_x = signs[1]["cx"] if len(signs) > 1 else sign1_x + (sign1_x - last_x)
	columns += [("sign_operator", sign1_x), ("sign_user", sign2_x)]

	# Description is the last, widest column: everything right of sign_user.
	columns.append(("desc", sign2_x + max(sign2_x - sign1_x, pitch) * 1.6))

	centres = [x for _, x in columns]
	if any(b <= a for a, b in zip(centres, centres[1:])):
		return None, ["The printed column headings were read in an unexpected order."]

	# Boundary to the right of column i: half the *narrower* neighbouring gap,
	# so a wide column (Description) doesn't swallow part of its neighbour.
	boundaries = []
	for i in range(len(columns) - 1):
		gap_next = centres[i + 1] - centres[i]
		gap_prev = centres[i] - centres[i - 1] if i > 0 else gap_next
		boundaries.append(centres[i] + min(gap_prev, gap_next) / 2.0)
	left_edge = centres[0] - (centres[1] - centres[0]) / 2.0

	return {"names": [n for n, _ in columns], "boundaries": boundaries, "left_edge": left_edge}, warnings


def _column_of(word, columns):
	if word["cx"] < columns["left_edge"]:
		return "left"
	for name, boundary in zip(columns["names"], columns["boundaries"]):
		if word["cx"] < boundary:
			return name
	return columns["names"][-1]


# ---------------------------------------------------------------------------
# 3. Locate the rows
# ---------------------------------------------------------------------------

def _day_index(word):
	n = _norm(word["text"])
	if len(n) < 5:
		return None
	scores = [(_similar(n, name), i) for i, name in enumerate(DAY_NAMES)]
	best, index = max(scores)
	return index if best >= 0.75 else None


def _weekly_row_anchors(words, header, columns):
	found = {}
	for w in words:
		if w["cy"] <= header["ref"]["cy"] or w["cx"] > columns["boundaries"][0]:
			continue
		index = _day_index(w)
		if index is not None and index not in found:
			found[index] = w["cy"]
	if len(found) < 2:
		return None

	# Least-squares line through (day number, y): fills in any day name OCR
	# missed, since the seven rows are printed evenly spaced.
	xs, ys = list(found.keys()), list(found.values())
	mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
	denom = sum((x - mean_x) ** 2 for x in xs)
	slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
	if slope <= 0:
		return None
	intercept = mean_y - slope * mean_x
	return [{"y": intercept + slope * i, "day_index": i} for i in range(7)], slope


def _dated_row_anchors(words, header, columns):
	"""Monthly sheet: one row per cluster of handwriting in the DATE column."""
	candidates = [
		w for w in words
		if w["cy"] > header["bottom"] and _column_of(w, columns) == "date" and re.search(r"\d", w["text"])
	]
	if not candidates:
		return None
	candidates.sort(key=lambda w: w["cy"])
	h_med = statistics.median(w["h"] for w in candidates)

	clusters, current = [], [candidates[0]]
	for w in candidates[1:]:
		if w["cy"] - current[-1]["cy"] > 0.6 * h_med:
			clusters.append(current)
			current = [w]
		else:
			current.append(w)
	clusters.append(current)
	ys = [statistics.mean(w["cy"] for w in cluster) for cluster in clusters]

	# The table ends where the regular row spacing stops (signatures and the
	# printed notes further down also contain digits, e.g. "5/09/26").
	if len(ys) >= 3:
		gaps = [b - a for a, b in zip(ys, ys[1:])]
		pitch = statistics.median(gaps)
		kept = [ys[0]]
		for y, gap in zip(ys[1:], gaps):
			if gap > 3.5 * pitch:
				break
			kept.append(y)
		ys = kept
		# One date written on a slant can split into two clusters; rejoin them.
		merged = [ys[0]]
		for y in ys[1:]:
			if y - merged[-1] < 0.55 * pitch:
				merged[-1] = (merged[-1] + y) / 2.0
			else:
				merged.append(y)
		ys = merged
	else:
		pitch = 2.0 * h_med
	return [{"y": y, "day_index": None} for y in ys], pitch


# ---------------------------------------------------------------------------
# 4. Cell parsers
# ---------------------------------------------------------------------------

def parse_hours(text):
	"""-> (value or None, note or None). A dash / blank cell is 0 hours."""
	t = (text or "").strip().replace(" ", "").replace(",", ".")
	if not t or all(ch in EMPTY_MARKS for ch in t):
		return 0.0, None
	match = re.fullmatch(r"(\d{1,2}(?:\.\d{1,2})?)\.?", t)
	note = None
	doubled = re.fullmatch(r"(\d{1,2})\1", t)
	if doubled and not match:
		# "2424": two neighbouring cells (Total 24, Normal 24) read as one word.
		return float(doubled.group(1)), f"read '{t}' as {doubled.group(1)}"
	if not match:
		mapped = t.translate(DIGIT_CONFUSIONS)
		match = re.search(r"(?<![\d.])\d{1,2}(?:\.\d{1,2})?(?![\d.])", mapped)
		if not match:
			return None, f"could not read '{text.strip()}' as hours"
		note = f"read '{text.strip()}' as {match.group(0)}"
		value = float(match.group(0))
	else:
		value = float(match.group(1))
	if value > 24:
		return None, f"'{text.strip()}' is more than 24 hours"
	return value, note


def parse_time(text):
	"""-> 'HH:MM:SS' or None. Accepts 8.00AM, 9:00 am, 11.00PM, 800AM, 14:30."""
	return parse_time_ex(text)[0]


def parse_time_ex(text):
	"""-> ('HH:MM:SS' or None, note or None). The note says what had to be
	assumed, so the row can be flagged for review."""
	t = (text or "").upper().strip()
	if not t:
		return None, None
	meridiem = re.search(r"([AP])\s*\.?\s*[MNH]", t)
	digits_part = t[: meridiem.start()] if meridiem else t
	trailing = t[meridiem.end():] if meridiem else ""
	digits_part = digits_part.replace("O", "0").replace("I", "1").replace("L", "1").replace("S", "5")
	digits_part = re.sub(r"&(?=[.:\-]?\d)", "8", digits_part)
	numbers = re.findall(r"\d+", digits_part) or re.findall(r"\d+", trailing.replace("O", "0"))
	if not numbers:
		return None, None

	if len(numbers) >= 2:
		hour, minute = numbers[0], numbers[1]
	elif len(numbers[0]) >= 3:
		hour, minute = numbers[0][:-2], numbers[0][-2:]
	else:
		hour, minute = numbers[0], "0"

	note = None
	if meridiem and len(hour) >= 2 and hour[0] == "1" and int(hour) > 12:
		# "1900 AM": the cell's ruled line was read as a leading 1.
		hour = hour[1:]
		note = f"read '{text.strip()}' as {int(hour)}:{minute[:2]}"
	try:
		hour, minute = int(hour), int(minute[:2])
	except ValueError:
		return None, None
	if hour > 23:
		return None, None
	if minute > 59:
		note = f"minutes in '{text.strip()}' are not valid, taken as :00"
		minute = 0

	if meridiem:
		if hour == 0:
			# There is no 0 o'clock on an AM/PM clock: the hour digit was lost
			# ("00AM" for "7.00AM") or misread ("0.00" for "9.00").
			return None, f"hour not readable in '{text.strip()}'"
		if hour <= 12:
			if meridiem.group(1) == "A":
				hour = 0 if hour == 12 else hour
			else:
				hour = 12 if hour == 12 else hour + 12
	return f"{hour:02d}:{minute:02d}:00", note


def duration_hours(from_time, to_time):
	"""Hours between two 'HH:MM:SS' strings; a To at or before From means the
	shift ran past midnight (8.00AM -> 8.00AM is a full 24-hour day)."""
	if not from_time or not to_time:
		return None
	fh, fm = int(from_time[:2]), int(from_time[3:5])
	th, tm = int(to_time[:2]), int(to_time[3:5])
	minutes = (th * 60 + tm) - (fh * 60 + fm)
	if minutes <= 0:
		minutes += 24 * 60
	return round(minutes / 60.0, 2)


def parse_date_parts(text):
	"""-> (day, month, year), each an int or None. Handles 14/08/26, 1/9/26,
	14|08|26, 14-08-2026, 140826 and separators misread as the digit 1
	(14108126)."""
	t = (text or "").strip()
	if not t:
		return None, None, None
	t = re.sub(r"(?<=\d)[|lI!\\\[\]()]+(?=\d)", "/", t)
	t = re.sub(r"(?<=[\d/])[Oo](?=[\d/])|(?<=[\d/])[Oo]$|^[Oo](?=\d)", "0", t)
	numbers = re.findall(r"\d+", t)
	if not numbers:
		return None, None, None

	candidates = []
	if len(numbers) >= 3:
		candidates.append((numbers[0], numbers[1], numbers[2]))
	if len(numbers) == 2:
		candidates.append((numbers[0], numbers[1], None))
	s = "".join(numbers) if len(numbers) != 2 else numbers[0]
	if len(numbers) == 1 or len(numbers) >= 3:
		if len(s) == 8:
			if s[2] == "1" and s[5] == "1":
				candidates.append((s[:2], s[3:5], s[6:]))
			candidates.append((s[:2], s[2:4], s[4:]))
		elif len(s) == 7:
			if s[2] == "1" and s[4] == "1":
				candidates.append((s[:2], s[3], s[5:]))
			if s[1] == "1" and s[4] == "1":
				candidates.append((s[0], s[2:4], s[5:]))
		elif len(s) == 6:
			candidates.append((s[:2], s[2:4], s[4:]))
			if s[1] == "1" and s[3] == "1":
				candidates.append((s[0], s[2], s[4:]))
		elif len(s) in (3, 4) and len(numbers) == 1:
			candidates.append((s[:2], s[2:], None))
	if len(numbers) == 1 and len(s) <= 2:
		candidates.append((s, None, None))

	best = (None, None, None)
	best_score = 0
	for d, m, y in candidates:
		day = int(d) if d and 1 <= int(d) <= 31 else None
		month = int(m) if m and 1 <= int(m) <= 12 else None
		year = None
		if y:
			value = int(y)
			if len(y) <= 2:
				value += 2000
			# A log sheet is for roughly "now"; a year far from today is a
			# misread digit ("2096"), so drop it and let the rest of the sheet
			# supply the year instead.
			if date.today().year - 6 <= value <= date.today().year + 1:
				year = value
		score = sum(part is not None for part in (day, month, year))
		if day is not None and score > best_score:
			best, best_score = (day, month, year), score
	return best


def _make_date(day, month, year):
	try:
		return date(year, month, day)
	except (TypeError, ValueError):
		return None


def date_time_candidates(text):
	"""Every way `text` can START with a date d/m/yy, as a list of
	(day, month, year, rest, separator_score).

	Built for what Vision really returns for handwritten dates on these ruled
	forms (seen on live scans): a '/' read as the digit 1 or dropped
	altogether, and the date glued to the time in the next column —
	"1419269.00" is 14/9/26 followed by 9.00, "719126900" is 7/9/26 then 900.
	`rest` is whatever follows the date (the From time, or "")."""
	s = re.sub(r"\s+", "", text or "")
	s = re.sub(r"[|lI!\\\[\]()]", "/", s).lstrip("/-.,:;'\"_")
	this_year = date.today().year

	def separator_options(pos):
		options = [(pos, 0.0)]
		if pos < len(s):
			if s[pos] in "/-.,":
				options.append((pos + 1, 1.0))
			elif s[pos] == "1":
				options.append((pos + 1, 0.5))
		return options

	found = []
	for day_len in (1, 2):
		day_text = s[:day_len]
		if len(day_text) < day_len or not day_text.isdigit():
			continue
		for after_sep1, score1 in separator_options(day_len):
			for month_len in (1, 2):
				month_text = s[after_sep1:after_sep1 + month_len]
				if len(month_text) < month_len or not month_text.isdigit():
					continue
				for after_sep2, score2 in separator_options(after_sep1 + month_len):
					for year_len in (2, 4):
						year_text = s[after_sep2:after_sep2 + year_len]
						if len(year_text) < year_len or not year_text.isdigit():
							continue
						day, month = int(day_text), int(month_text)
						year = int(year_text) + (2000 if year_len == 2 else 0)
						if not (this_year - 6 <= year <= this_year + 1):
							continue
						if _make_date(day, month, year) is None:
							continue
						found.append((day, month, year, s[after_sep2 + year_len:], score1 + score2))
	return found


def _best_date_candidate(candidates, expected):
	"""expected = (month, year) the sheet is for (either may be None)."""
	if not candidates:
		return None

	def score(candidate):
		_day, month, year, rest, separators = candidate
		points = separators
		if expected and expected[0] and month == expected[0]:
			points += 3
		if expected and expected[1] and year == expected[1]:
			points += 3
		if not rest or parse_time(rest):
			points += 1
		return points

	return max(candidates, key=score)


def parse_month_text(text):
	"""'September-26' / 'AUGUST 2026' -> (9, 2026). Either part may be None."""
	month = year = None
	for letters in re.findall(r"[A-Za-z]{3,}", text or ""):
		lowered = letters.lower()
		for index, name in enumerate(MONTH_NAMES):
			if lowered[:3] == name[:3].lower() or _similar(lowered, name.lower()) >= 0.75:
				month = index + 1
				break
		if month:
			break
	for digits in re.findall(r"\d+", text or ""):
		value = int(digits) + (2000 if len(digits) == 2 else 0)
		if len(digits) in (2, 4) and date.today().year - 6 <= value <= date.today().year + 1:
			year = value
			break
	return month, year


# ---------------------------------------------------------------------------
# 5. Rows
# ---------------------------------------------------------------------------

def _cell_confidence(cells):
	scores = [
		w["conf"] for name in ("date", "from", "to", "total", "normal", "ot", "break")
		for w in cells.get(name, []) if w.get("conf") is not None
	]
	return statistics.mean(scores) if scores else DEFAULT_WORD_CONFIDENCE


def _build_rows(words, header, columns, anchors, pitch, template, expected=None):
	"""expected: (month, year) read from the sheet's MONTH box, if any."""
	ys = [a["y"] for a in anchors]
	top = max(header["ref"]["cy"] + 0.8 * header["h"], ys[0] - 0.6 * pitch)
	bottom = ys[-1] + 0.6 * pitch
	cells_by_row = [dict() for _ in anchors]
	for w in words:
		if not (top <= w["cy"] <= bottom) or id(w) in header["printed_ids"]:
			continue
		nearest = min(range(len(ys)), key=lambda i: abs(ys[i] - w["cy"]))
		if abs(ys[nearest] - w["cy"]) > 0.6 * pitch:
			continue
		column = _column_of(w, columns)
		cells_by_row[nearest].setdefault(column, []).append(w)

	# Vision often glues the date to the From time across the column line, so
	# the two cells are read as one string and split again by date_time_candidates.
	combined = [_join(cells.get("date", []) + cells.get("from", [])) for cells in cells_by_row]
	candidates = [date_time_candidates(text) for text in combined]
	if not expected or not all(expected):
		# No (complete) MONTH box: the month/year most rows can agree on.
		votes = [pair for row in candidates for pair in {(c[1], c[2]) for c in row}]
		voted = statistics.multimode(votes)[0] if votes else (None, None)
		expected = ((expected or (None, None))[0] or voted[0], (expected or (None, None))[1] or voted[1])

	rows = []
	for anchor, cells, row_candidates in zip(anchors, cells_by_row, candidates):
		notes = []
		date_text = _join(cells.get("date", []))
		chosen = _best_date_candidate(row_candidates, expected)
		if chosen:
			day, month, year, from_text = chosen[0], chosen[1], chosen[2], chosen[3]
		else:
			day, month, year = parse_date_parts(date_text)
			from_text = _join(cells.get("from", []))

		ruling = re.match(r"^[/|]+1(\d{3})(?!\d)", from_text)
		if ruling:
			# "/1900": the column line between Date and From read as "/1".
			from_text = ruling.group(1) + from_text[ruling.end():]
			notes.append("time: from read with the column line ignored")
		from_time, from_note = parse_time_ex(from_text)
		to_time, to_note = parse_time_ex(_join(cells.get("to", [])))
		if (cells.get("from") or cells.get("to")) and not (from_time and to_time):
			# Handwriting often straddles the From|To ruling; read both cells together.
			joined = from_text + _join(cells.get("to", []))
			both = re.findall(r"\d{1,2}\s*[:.\-,]?\s*\d{0,2}\s*[AaPp]\s*\.?\s*[MmNnHh]", joined)
			if len(both) == 2 and parse_time(both[0]) and parse_time(both[1]):
				(from_time, from_note), (to_time, to_note) = parse_time_ex(both[0]), parse_time_ex(both[1])
		for label, note in (("from", from_note), ("to", to_note)):
			if note:
				notes.append(f"time: {label} {note}")

		hours = {}
		for key in ("total", "normal", "ot", "break"):
			value, note = parse_hours(_join(cells.get(key, [])))
			hours[key] = value
			if note:
				notes.append(f"{key}: {note}")

		description_words = [w for w in cells.get("desc", []) if not _is_foreign_script(w["text"])]
		description = " ".join(w["text"] for w in sorted(description_words, key=lambda w: (round(w["cy"] / max(pitch * 0.4, 1)), w["cx"])))
		description = re.sub(r"\s+", " ", description).strip(" -–—_.=") or None

		has_date = day is not None
		has_time_text = bool(from_text.strip() or cells.get("to"))
		has_content = bool(from_time or to_time or has_time_text or any(hours.get(k) for k in hours) or hours.get("total") is None and cells.get("total"))
		if not has_date and not has_content:
			continue
		if has_date and not has_content and not description:
			# A date with nothing else on the line (e.g. the unused rows of a
			# struck-through sheet): not a worked day.
			continue

		rows.append({
			"day_index": anchor["day_index"],
			"date_parts": (day, month, year),
			"date_text": date_text,
			"from_time": from_time,
			"to_time": to_time,
			"hours": hours,
			"work_description": description,
			"vision_confidence": _cell_confidence(cells),
			"notes": notes,
			"has_time_text": has_time_text,
		})
	return rows


def _resolve_dates(rows, template):
	"""Turns each row's (day, month, year) reading into a real date, using the
	rest of the sheet as context: a missing month/year is taken from the
	sheet's most common one, and on a Weekly sheet the seven rows must be
	consecutive days, so one misread date is corrected from its neighbours."""
	full = [(r["date_parts"][1], r["date_parts"][2]) for r in rows if all(p is not None for p in r["date_parts"])]
	common = statistics.multimode(full)[0] if full else (None, None)

	for row in rows:
		day, month, year = row["date_parts"]
		row["log_date"] = None
		if day is None:
			continue
		if month is None or year is None:
			if common[0] is None:
				row["notes"].append(f"date: only the day could be read from '{row['date_text']}'")
				continue
			month, year = (month or common[0]), (year or common[1])
			row["notes"].append(f"date: month/year assumed from the rest of the sheet ('{row['date_text']}')")
		row["log_date"] = _make_date(day, month, year)
		if row["log_date"] is None:
			row["notes"].append(f"date: '{row['date_text']}' is not a valid date")

	if template == "Weekly" and all(r["day_index"] is not None for r in rows):
		bases = [r["log_date"] - timedelta(days=r["day_index"]) for r in rows if r["log_date"]]
		if bases:
			votes = statistics.multimode(bases)
			base = votes[0]
			agree = sum(1 for b in bases if b == base)
			if agree >= 2 or len(bases) == 1:
				for row in rows:
					expected = base + timedelta(days=row["day_index"])
					if row["log_date"] != expected:
						if len(bases) > 1 or row["log_date"] is None:
							was = row["log_date"].isoformat() if row["log_date"] else (row["date_text"] or "blank")
							row["notes"].append(f"date: set to {expected.isoformat()} to follow the day sequence (read as '{was}')")
							row["log_date"] = expected
	else:
		# Monthly: only repair a single bad/missing date squeezed between two
		# good ones that are exactly two days apart.
		for i in range(1, len(rows) - 1):
			before, after = rows[i - 1]["log_date"], rows[i + 1]["log_date"]
			if before and after and (after - before).days == 2:
				expected = before + timedelta(days=1)
				if rows[i]["log_date"] != expected:
					was = rows[i]["log_date"].isoformat() if rows[i]["log_date"] else (rows[i]["date_text"] or "blank")
					rows[i]["notes"].append(f"date: set to {expected.isoformat()} to follow the day sequence (read as '{was}')")
					rows[i]["log_date"] = expected
		previous = None
		for row in rows:
			if row["log_date"] and previous and row["log_date"] <= previous:
				row["notes"].append("date: not later than the row above — check")
			previous = row["log_date"] or previous


def _move_misplaced_overtime(rows, template):
	"""Weekly sheets only. Some operators write the overtime figure in the
	'Normal Shift Hours' column and leave 'Over-Time Hours' empty (seen on a
	live sheet: Total 13 / Normal 1, Total 16 / Normal 4, ...). When NO row
	has overtime and the 'normal' figures are too small to be a normal shift,
	treat them as the overtime they evidently are — and say so, so the row
	is still reviewed."""
	if template != "Weekly" or any(row["hours"].get("ot") for row in rows):
		return
	misplaced = [
		row for row in rows
		if row["hours"].get("normal") and row["hours"].get("total") and row["hours"]["normal"] <= row["hours"]["total"] / 2.0
	]
	if len(misplaced) < 2 or any(
		row["hours"].get("normal") and row not in misplaced for row in rows
	):
		return
	for row in misplaced:
		row["hours"]["ot"], row["hours"]["normal"] = row["hours"]["normal"], 0.0
		row["notes"].append(
			f"ot: {row['hours']['ot']:g} was written in the Normal Shift column on the sheet; taken as overtime"
		)


def _flag_odd_blanks(rows):
	"""A Normal Shift cell that is blank on one row but filled on most of the
	sheet was most likely missed by OCR rather than left empty."""
	filled = [row for row in rows if row["hours"].get("normal")]
	if len(rows) >= 4 and len(filled) >= 0.6 * len(rows):
		for row in rows:
			if row["hours"].get("normal") == 0 and row["hours"].get("total"):
				row["notes"].append("normal: blank here but filled on the other rows — check")


def _finalise_row(row):
	hours, notes = row["hours"], row["notes"]
	confidence = row["vision_confidence"]
	span = duration_hours(row["from_time"], row["to_time"])

	if row["log_date"] is None:
		confidence = 0.0
	if any(note.startswith("date:") for note in notes):
		confidence = min(confidence, 0.5)

	if row["has_time_text"] and not (row["from_time"] and row["to_time"]):
		notes.append("time: From/To could not be fully read")
		confidence = min(confidence, 0.5)

	total = hours.get("total")
	if total is None or (total == 0 and span):
		if span:
			notes.append(f"total: not readable, set to {span:g} from the From–To times")
			total = span
			confidence = min(confidence, 0.4)
		else:
			total = 0.0
			confidence = min(confidence, 0.2)
	elif span is not None and abs(span - total) > 0.5:
		notes.append(f"total: {total:g} h does not match From–To ({span:g} h)")
		confidence = min(confidence, 0.5)

	normal = hours.get("normal")
	overtime = hours.get("ot")
	breakdown = hours.get("break")
	if normal is None or overtime is None or breakdown is None:
		confidence = min(confidence, 0.5)
	normal, overtime, breakdown = normal or 0.0, overtime or 0.0, breakdown or 0.0

	if normal and total and abs((normal + overtime) - total) > 0.5:
		# Also catches overtime written in the Normal Shift column by mistake.
		notes.append(f"hours: normal {normal:g} + overtime {overtime:g} does not equal total {total:g} (value in the wrong column?)")
		confidence = min(confidence, 0.6)
	if any(": read '" in note or "taken as" in note or " read '" in note or "column line" in note or "— check" in note for note in notes):
		confidence = min(confidence, 0.6)
	if not notes and row["log_date"] and span is not None and total and abs(span - total) <= 0.5:
		# Handwriting gets a modest per-word score from Vision even when it is
		# read correctly. A row whose date fits the sequence and whose Total
		# equals its own From–To span has been confirmed twice over.
		confidence = max(confidence, 0.85)

	day_label = DAY_NAMES[row["day_index"]].title() if row["day_index"] is not None else None
	if day_label is None and row["log_date"]:
		day_label = DAY_NAMES[row["log_date"].weekday()].title()

	return {
		"day_label": day_label,
		"log_date": row["log_date"].isoformat() if row["log_date"] else None,
		"from_time": row["from_time"],
		"to_time": row["to_time"],
		"total_hours": total,
		"normal_shift_hours": normal,
		"overtime_hours": overtime,
		"breakdown_hours": breakdown,
		"work_description": row["work_description"],
		"confidence": round(max(0.0, min(1.0, confidence)), 2),
		"notes": notes,
	}


# ---------------------------------------------------------------------------
# 6. Printed header block (Operator, Crane Model, Regn. No., ...)
# ---------------------------------------------------------------------------

TITLE_TOKENS = {"weekly", "monthly", "crane", "log", "sheet", "logsheet", "copy", "w", "o", "wo", "no", "wono", "sr", "srno", "nosr"}


def _find_labels(words, limit_y):
	"""-> list of {"key", "x1" (right edge), "x0", "cy", "h"} for the printed
	labels above the table."""
	top = sorted((w for w in words if w["cy"] < limit_y), key=lambda w: (w["cy"], w["cx"]))
	labels, used = [], set()

	def same_line_right(word, max_gap):
		return [
			w for w in top
			if w is not word and abs(w["cy"] - word["cy"]) < 0.7 * word["h"] and 0 <= w["x0"] - word["x1"] < max_gap
		]

	for w in top:
		n = _norm(w["text"])
		key, right = None, w["x1"]
		if n == "operator":
			key = "operator"
		elif n == "model":
			key = "crane_model"
		elif n.startswith("regn"):
			key = "regn_no"
			# swallow the rest of "REGN. NO./SR. NO." so it isn't read as the value
			cursor, guard = w, 0
			while guard < 8:
				guard += 1
				following = [x for x in same_line_right(cursor, 2.5 * w["h"]) if _norm(x["text"]) in ("", "no", "sr", "srno", "nosr", "nosrno")]
				if not following:
					break
				cursor = min(following, key=lambda x: x["x0"])
				used.add(id(cursor))
				right = max(right, cursor["x1"])
		elif n == "month":
			key = "month_text"
		elif n == "site":
			key = "site"
		elif n == "client":
			if any(_norm(x["text"]) == "copy" for x in same_line_right(w, 3 * w["h"])):
				continue
			key = "client"
		elif n in ("no", "nos") or n.startswith("no"):
			left = [x for x in top if abs(x["cy"] - w["cy"]) < 0.7 * w["h"] and 0 <= w["x0"] - x["x1"] < 2.5 * w["h"]]
			if any(_norm(x["text"]) == "sheet" for x in left):
				key = "log_sheet_no"
		if key and not any(label["key"] == key for label in labels):
			labels.append({"key": key, "x0": w["x0"], "x1": right, "cy": w["cy"], "h": w["h"]})
			used.add(id(w))
	return labels, used


def _read_header_block(words, header, page_width):
	limit_y = header["block_limit"] if header else max((w["y1"] for w in words), default=0) * 0.35
	labels, used = _find_labels(words, limit_y)
	if not labels:
		return {}

	# Two side-by-side stacks of labels (Operator/Model/Regn on the left,
	# Month/Site/Client on the right).
	split = page_width * 0.45
	groups = {
		0: [label for label in labels if label["x0"] < split],
		1: [label for label in labels if label["x0"] >= split],
	}
	pitches = {}
	for g, members in groups.items():
		ys = sorted(label["cy"] for label in members)
		gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0]
		pitches[g] = statistics.median(gaps) if gaps else (2.0 * members[0]["h"] if members else 0)

	values = {label["key"]: [] for label in labels}
	for w in words:
		if w["cy"] >= limit_y or id(w) in used:
			continue
		n = _norm(w["text"])
		if n in TITLE_TOKENS or n in ("operator", "model", "month", "site", "client"):
			continue
		if not n and w["text"].strip() not in ("-", "/", ".", "&"):
			continue
		if _is_foreign_script(w["text"]):
			continue
		eligible = [g for g, members in groups.items() if members and min(label["x1"] for label in members) < w["cx"]]
		# Something written on the right half of the page (e.g. under the
		# printed "W. O. NO.") never belongs to a left-hand label.
		if w["cx"] >= split and groups[1]:
			eligible = [g for g in eligible if g == 1]
		if not eligible:
			continue
		g = max(eligible)
		best, best_distance = None, None
		for label in groups[g]:
			if label["x1"] >= w["cx"]:
				continue
			# Handwriting usually sits a little below the printed label's line.
			distance = abs(w["cy"] - (label["cy"] + 0.15 * pitches[g]))
			if distance <= 0.62 * pitches[g] and (best is None or distance < best_distance):
				best, best_distance = label, distance
		if best:
			values[best["key"]].append(w)

	out = {}
	for key, found in values.items():
		if not found:
			continue
		text = " ".join(w["text"] for w in sorted(found, key=lambda w: w["cx"]))
		text = re.sub(r"\s*([-/.])\s*", r"\1", text)
		text = re.sub(r"\s+", " ", text).strip(" :;.-–—_=|/&")
		if not text:
			continue
		if key == "log_sheet_no":
			run = re.search(r"\d{3,6}", text) or re.search(r"\d{3,6}", text.translate(DIGIT_CONFUSIONS))
			text = run.group(0) if run else None
		elif key == "regn_no":
			text = re.sub(r"[\s|]", "", text).upper()
		if text:
			out[key] = text
	return out


def _read_hour_meter(words, below_y):
	"""Weekly sheet footer: 'HOUR METER READING : OPENING = 31756  CLOSING 31804'."""
	out = {}
	footer = [w for w in words if w["cy"] > below_y]
	for key, label in (("hour_meter_opening", "opening"), ("hour_meter_closing", "closing")):
		anchor = next((w for w in footer if _norm(w["text"]).startswith(label)), None)
		if not anchor:
			continue
		right = sorted(
			(w for w in footer if w["x0"] >= anchor["x1"] - 2 and abs(w["cy"] - anchor["cy"]) < 0.8 * anchor["h"]),
			key=lambda w: w["x0"],
		)
		digits = ""
		for w in right:
			n = _norm(w["text"])
			if n.startswith(("closing", "total", "in", "words")):
				break
			if w["x0"] - anchor["x1"] > 14 * anchor["h"]:
				break
			chunk = re.sub(r"\D", "", w["text"].translate(DIGIT_CONFUSIONS)) if re.search(r"\d", w["text"]) else ""
			digits += chunk
			if len(digits) >= 5:
				break
		if 3 <= len(digits) <= 7:
			out[key] = float(digits)
	return out


def _read_footer_totals(words, below_y):
	"""The sheet's own handwritten totals under the table — 'TOTAL NO. OF
	WORKING DAYS / SHIFTS = 6' and 'TOTAL NO. OF OVERTIME HOURS = 25' — used
	only to cross-check what was read from the rows."""
	footer = [w for w in words if w["cy"] > below_y]
	out = {}
	for key, starts in (("sheet_working_days", ("shifts", "shiftq")), ("sheet_overtime_hours", ("overtime",))):
		anchor = next((w for w in footer if _norm(w["text"]).startswith(starts)), None)
		if not anchor:
			continue
		right = sorted(
			(w for w in footer if w["x0"] >= anchor["x1"] - 2 and abs(w["cy"] - anchor["cy"]) < 0.8 * anchor["h"]),
			key=lambda w: w["x0"],
		)
		for w in right:
			n = _norm(w["text"])
			if w["text"].strip().startswith("(") or n in ("in", "words", "total") or w["x0"] - anchor["x1"] > 14 * anchor["h"]:
				break
			if n in ("hours", "") or not re.search(r"[0-9A-Za-z]", w["text"]):
				continue
			digits = re.fullmatch(r"\d{1,3}", w["text"].strip().translate(DIGIT_CONFUSIONS))
			if digits:
				out[key] = float(digits.group(0))
			break
	return out


def _month_from_page(words, header, limit_y):
	"""(month, year) the sheet is for: the MONTH box if it was read, else any
	month name written above the table."""
	month, year = parse_month_text(header.get("month_text"))
	if month and year:
		return month, year
	top = [w for w in words if w["cy"] < limit_y]
	for w in top:
		found_month, _ = parse_month_text(w["text"]) if len(_norm(w["text"])) >= 3 else (None, None)
		if not found_month:
			continue
		nearby = " ".join(
			x["text"] for x in sorted(top, key=lambda x: x["cx"])
			if x is not w and abs(x["cy"] - w["cy"]) < 2.0 * w["h"] and x["cx"] > w["x0"]
		)
		_, found_year = parse_month_text(w["text"] + " " + nearby)
		return month or found_month, year or found_year
	return month, year


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def detect_template(words):
	norms = [_norm(w["text"]) for w in words]
	if "weekly" in norms:
		return "Weekly"
	if "monthly" in norms:
		return "Monthly"
	day_hits = sum(1 for w in words if _day_index(w) is not None)
	if day_hits >= 3:
		return "Weekly"
	return None


def parse_sheet(words):
	"""-> {"template", "header", "rows", "warnings", "period_start",
	"period_end", "month"}. Never raises on a merely unreadable page: it
	returns zero rows plus a warning saying what could not be found."""
	result = {
		"template": None, "header": {}, "rows": [], "warnings": [],
		"period_start": None, "period_end": None, "month": None,
	}
	if not words:
		result["warnings"].append("No text was recognised on this page.")
		return result

	page_width = max(w["x1"] for w in words)
	template = detect_template(words)
	result["template"] = template
	if template is None:
		result["warnings"].append("Could not tell whether this is a Weekly or a Monthly sheet (title not readable).")

	header = _find_header(words)
	result["header"] = _read_header_block(words, header, page_width)
	if not header:
		result["warnings"].append("Could not find the table's printed column headings — no rows were read.")
		return result

	columns, column_warnings = _columns(header, template)
	result["warnings"] += column_warnings
	if not columns:
		return result

	located = None
	if template != "Monthly":
		located = _weekly_row_anchors(words, header, columns)
		if located and template is None:
			template = result["template"] = "Weekly"
	if not located:
		located = _dated_row_anchors(words, header, columns)
		if located and template is None:
			template = result["template"] = "Monthly"
	if not located:
		result["warnings"].append("The table heading was found but no filled-in rows were recognised under it.")
		return result
	anchors, pitch = located

	expected = _month_from_page(words, result["header"], header["block_limit"])
	rows = _build_rows(words, header, columns, anchors, pitch, template, expected)
	_resolve_dates(rows, template)
	_move_misplaced_overtime(rows, template)
	_flag_odd_blanks(rows)
	result["rows"] = [_finalise_row(row) for row in rows]
	if not result["rows"]:
		result["warnings"].append("The table was found but every row looked empty.")

	table_bottom = anchors[-1]["y"] + 0.5 * pitch
	if template == "Weekly":
		result["header"].update(_read_hour_meter(words, table_bottom))

	# Cross-check the rows against the totals handwritten under the table.
	totals = _read_footer_totals(words, table_bottom)
	result["header"].update(totals)
	if result["rows"]:
		overtime_read = sum(r["overtime_hours"] for r in result["rows"])
		if "sheet_overtime_hours" in totals and abs(totals["sheet_overtime_hours"] - overtime_read) > 0.5:
			result["warnings"].append(
				f"The sheet's own 'Total No. of Overtime Hours' is {totals['sheet_overtime_hours']:g} but the rows read "
				f"add up to {overtime_read:g} — an overtime entry was probably missed, misread or written in another column."
			)
		if "sheet_working_days" in totals and int(totals["sheet_working_days"]) != len(result["rows"]):
			result["warnings"].append(
				f"The sheet's own 'Total No. of Working Days' is {totals['sheet_working_days']:g} but {len(result['rows'])} "
				"row(s) were read."
			)

	dates = sorted(r["log_date"] for r in result["rows"] if r["log_date"])
	if dates:
		result["period_start"], result["period_end"] = dates[0], dates[-1]
		first = date.fromisoformat(dates[0])
		result["month"] = f"{MONTH_NAMES[first.month - 1]} {first.year}"
	return result
