
import os
import json
import csv
from dateutil import parser

def load_creditors(file_path):
    with open(file_path, "r") as f:
        raw_map = json.load(f)
    name_to_primary = {}
    for primary, aliases in raw_map.items():
        name_to_primary[primary.strip().lower()] = primary
        for alias in aliases:
            name_to_primary[alias.strip().lower()] = primary
    return name_to_primary

def load_bank_statement(file_path):
    with open(file_path, "r") as f:
        reader = csv.DictReader(f)
        return [
            {
                "document_id": int(row["document_id"]),
                "vendor": row["vendor"],
                "amount": float(row["amount"]),
                "bank_date": parser.parse(row["bank_date"]).date()
            }
            for row in reader
        ]

def save_json(data, filename):
    with open(filename, "w") as f:
        json.dump(data, f, indent=2, default=str)
