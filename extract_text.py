
import pytesseract
from PIL import Image
import fitz  # PyMuPDF
import re
import os
import cv2
import numpy as np
from datetime import datetime

DANISH_MONTHS = {
    "januar": "01", "februar": "02", "marts": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12"
}


# match Danish style 1.234,56
DANISH_AMT = re.compile(r'^\d{1,3}(?:\.\d{3})*,\d{2}$')
# match US style    1,234.56
US_AMT     = re.compile(r'^\d{1,3}(?:,\d{3})*\.\d{2}$')

def normalize_amount(token: str) -> float | None:
    t = token.strip()
    if DANISH_AMT.fullmatch(t):
        # remove thousand‐sep (“.”), swap decimal “,” → “.”
        norm = t.replace('.', '').replace(',', '.')
    elif US_AMT.fullmatch(t):
        # remove thousand‐sep (“,”), leave decimal “.”
        norm = t.replace(',', '')
    else:
        return None
    try:
        return float(norm)
    except ValueError:
        return None

def preprocess_image(pil_image):
    image = np.array(pil_image.convert("RGB"))
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    gray = cv2.bilateralFilter(gray, d=9, sigmaColor=75, sigmaSpace=75)

    coords = np.column_stack(np.where(gray < 255))
    angle = 0.0
    if coords.any():
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        (h, w) = gray.shape
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    processed = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        61, 10
    )

    return Image.fromarray(processed)

def extract_text_from_image(file_path):
    try:
        pil_image = Image.open(file_path)
        processed_image = preprocess_image(pil_image)
        text = pytesseract.image_to_string(processed_image, lang="dan")
        if len(text.strip()) < 10:
            text = pytesseract.image_to_string(processed_image, lang="eng")
        return text
    except Exception as e:
        print(f"Error processing image {file_path}: {e}")
        return ""

def extract_text_from_pdf(file_path):
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        print(f"Error processing PDF {file_path}: {e}")
        return ""

def extract_text(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext in [".jpg", ".jpeg", ".png"]:
        return extract_text_from_image(file_path)
    elif ext == ".pdf":
        return extract_text_from_pdf(file_path)
    else:
        return ""

def find_dates(text):
    dates = set()
    date_substrings = set()

    # dd.mm.yyyy and dd-mm-yyyy
    d1 = re.findall(r"\b(\d{2}[.-]\d{2}[.-]\d{4})\b", text)
    dates.update(d1)
    date_substrings.update(d1)

    # dd mm yyyy
    d2 = re.findall(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", text)
    for day, month, year in d2:
        formatted = f"{int(day):02d}.{int(month):02d}.{year}"
        dates.add(formatted)
        date_substrings.add(f"{day} {month} {year}")

    # dd. full month name yyyy
    d3 = re.findall(r"(\d{1,2})\.\s*([a-zA-ZæøåÆØÅ]+)\s+(\d{4})", text)
    for day, month_name, year in d3:
        month_num = DANISH_MONTHS.get(month_name.strip().lower())
        if month_num:
            formatted = f"{int(day):02d}.{month_num}.{year}"
            date_substrings.add(f"{day}. {month_name} {year}".lower())
            dates.add(formatted)

    # dd. 3-letter month abbreviation yyyy
    d4 = re.findall(r"(\d{1,2})\.\s*(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)\s+(\d{4})", text, re.IGNORECASE)
    MONTH_ABBR = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "maj": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "okt": "10", "nov": "11", "dec": "12"
    }
    for day, abbr, year in d4:
        month_num = MONTH_ABBR.get(abbr.lower())
        if month_num:
            formatted = f"{int(day):02d}.{month_num}.{year}"
            dates.add(formatted)
            date_substrings.add(f"{day}. {abbr} {year}".lower())

    return list(dates), date_substrings

def find_amounts(text: str) -> list[float]:
    """
    Scan OCR text for numeric tokens, normalize either Danish or US style,
    and return a list of floats.
    """
    # grab any run of digits, dots or commas (you can keep your old TOKEN regex)
    tokens = re.findall(r'(?<!\d)([\d\.,]+)(?!\d)', text)
    amounts = []
    for tok in tokens:
        val = normalize_amount(tok)
        if val is not None:
            amounts.append(val)
    return amounts


from rapidfuzz import process, fuzz

def find_vendor(text, known_creditors):
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    vendor_matches = {}

    for line in lines:
        for primary, aliases in known_creditors.items():
            all_names = [primary.lower()] + [a.lower() for a in aliases]
            match, score, _ = process.extractOne(line, all_names, scorer=fuzz.token_sort_ratio)
            if score >= 80:  # Threshold
                if primary not in vendor_matches or vendor_matches[primary]["score"] < score:
                    vendor_matches[primary] = {"score": score, "line": line}

    if not vendor_matches:
        return None

    best_match = max(vendor_matches.items(), key=lambda x: x[1]["score"])
    result = {
        "primary_vendor": best_match[0],
        "score": best_match[1]["score"],
        "matched_line": best_match[1]["line"],
        "alternatives": [
            {"primary_vendor": k, "score": v["score"], "matched_line": v["line"]}
            for k, v in vendor_matches.items() if k != best_match[0]
        ]
    }
    return result


def extract_raw_text_lines(text, date_tokens, amounts):
    lines = [line.strip() for line in text.splitlines() if re.search(r"[A-Za-zæøåÆØÅ]{3,}", line)]
    filtered_lines = []
    for line in lines:
        if any(d.lower() in line.lower() for d in date_tokens):
            continue
        if any(f"{amt:.2f}".replace(".", ",") in line or f"{amt:.2f}" in line for amt in amounts):
            continue
        filtered_lines.append(line)
    return filtered_lines
