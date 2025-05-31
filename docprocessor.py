#!/usr/bin/env python3
import os
import argparse
from pathlib import Path

from extract_text import extract_text
from data_extractors import extract_data_from_text
from io_utils import save_json

def main(input_dir: Path, output_json: Path):
    """
    Walks the input directory, runs OCR/text extraction on each supported file,
    then identifies dates, amounts, and a vendor line in the extracted text.
    Outputs a JSON array to output_json with one entry per document.
    """
    docs_data = []
    SUPPORTED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png'}

    for file_path in sorted(input_dir.iterdir()):
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        # 1) Extract raw text with the tuned OCR logic
        text = extract_text(str(file_path))

        # 2) Identify dates, amounts, and vendor from the text
        extracted = extract_data_from_text(text)

        # 3) Build record with ISO-formatted dates
        docs_data.append({
            "file":    file_path.name,
            "dates":   [d.isoformat() for d in extracted.get("dates", [])],
            "amounts": extracted.get("amounts", []),
            "vendors":  extracted.get("vendors", [])
        })

    # 4) Save all results to a JSON file
    save_json(docs_data, output_json)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract dates, amounts, and vendor info from documents"
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing documents (PDF, JPG, PNG) to process"
    )
    parser.add_argument(
        "output_json",
        type=Path,
        help="Path to write the JSON output (e.g. docdata.json)"
    )
    args = parser.parse_args()
    main(args.input_dir, args.output_json)
