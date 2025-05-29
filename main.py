
import os
import json
import pandas as pd
from datetime import datetime
from extract_text import extract_text, find_dates, find_amounts, find_vendor, extract_raw_text_lines

DOCUMENTS_DIR = "documents"
STATEMENT_FILE = "bank_statement.csv"
CREDITORS_FILE = "creditors.json"
DATE_TOLERANCE_DAYS = 3

bank_df = pd.read_csv(STATEMENT_FILE)
bank_df["bank_date"] = pd.to_datetime(bank_df["bank_date"], dayfirst=True)

with open(CREDITORS_FILE, "r", encoding="utf-8") as f:
    creditor_map = json.load(f)

documents_info = []
unrecognized_creditors = set()

for filename in os.listdir(DOCUMENTS_DIR):
    filepath = os.path.join(DOCUMENTS_DIR, filename)
    if not os.path.isfile(filepath):
        continue

    text = extract_text(filepath)
    dates, date_tokens = find_dates(text)
    amounts = find_amounts(text, exclude_substrings=date_tokens)
    vendor = find_vendor(text, creditor_map)
    raw_lines = extract_raw_text_lines(text, date_tokens, amounts)

    if vendor is None:
        for line in raw_lines:
            unrecognized_creditors.add(line.strip())

    documents_info.append({
        "filename": filename,
        "dates": dates,
        "amounts": amounts,
        "vendor": vendor,
        "raw_text_lines": raw_lines
    })

matches = []
unmatched_documents = []
matched_doc_filenames = set()
matched_record_ids = set()

for doc in documents_info:
    matched = False
    if not doc["vendor"] or not doc["amounts"]:
        unmatched_documents.append(doc)
        continue

    max_amount = max(doc["amounts"])
    for idx, record in bank_df.iterrows():
        if record["document_id"] in matched_record_ids:
            continue
        if record["amount"] != max_amount:
            continue
        if record["vendor"] != doc["vendor"]:
            continue

        doc_dates = [datetime.strptime(d, "%d.%m.%Y") for d in doc["dates"] if "." in d]
        if any(abs((record["bank_date"] - d).days) <= DATE_TOLERANCE_DAYS for d in doc_dates):
            matches.append({
                "filename": doc["filename"],
                "record": {
                    **record.drop(labels=["bank_date"]).to_dict(),
                    "bank_date": record["bank_date"].strftime("%Y-%m-%d")
                }
            })
            matched_doc_filenames.add(doc["filename"])
            matched_record_ids.add(record["document_id"])
            matched = True
            break

    if not matched:
        unmatched_documents.append(doc)

unmatched_records = bank_df[~bank_df["document_id"].isin(matched_record_ids)].copy()
unmatched_records["bank_date"] = unmatched_records["bank_date"].dt.strftime("%Y-%m-%d")
unmatched_records_list = unmatched_records.to_dict(orient="records")

with open("matches.json", "w", encoding="utf-8") as f:
    json.dump(matches, f, indent=2, ensure_ascii=False)

with open("unmatched_documents.json", "w", encoding="utf-8") as f:
    json.dump(unmatched_documents, f, indent=2, ensure_ascii=False)

with open("unmatched_records.json", "w", encoding="utf-8") as f:
    json.dump(unmatched_records_list, f, indent=2, ensure_ascii=False)

with open("unrecognized_creditors.json", "w", encoding="utf-8") as f:
    json.dump(sorted(unrecognized_creditors), f, indent=2, ensure_ascii=False)
