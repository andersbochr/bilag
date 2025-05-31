# data_extractors.py

from dateutil import parser
from extract_text import find_dates, find_amounts, extract_raw_text_lines

def extract_data_from_text(text):
    """
    Given raw OCR text, extract:
      - a list of dates (as datetime.date)
      - a list of amounts (as float)
      - a list of all vendor‐candidate lines (excluding any line containing a date or amount)
    """

    # 1) Use your tuned finder to get both formatted dates and the raw substrings to exclude
    date_strings, date_substrings = find_dates(text)  
    # Convert the formatted dates (e.g. "01.02.2024") into date objects
    dates = []
    for ds in date_strings:
        try:
            # replace '.' with '-' so parser can handle it, e.g. "01.02.2024" -> "01-02-2024"
            dates.append(parser.parse(ds.replace('.', '-'), dayfirst=True).date())
        except Exception:
            continue

    # 2) Use your tuned finder to extract all amounts (floats)
    amounts = find_amounts(text)  

    # 3) Now pull out every line of text that:
    #    a) contains at least one letter
    #    b) does *not* contain any of the raw date substrings
    #    c) does *not* contain any of the amounts (formatted either "1.234,56" or "1234.56")
    vendor_lines = extract_raw_text_lines(text, date_substrings, amounts)  

    return {
        "dates":   dates,
        "amounts": amounts,
        "vendors": [line.strip() for line in vendor_lines]
    }
