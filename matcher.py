#!/usr/bin/env python3
import sys
import json
import csv
import re
from pathlib import Path
import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

# -----------------------------------------------------------------------------
#  Matching passes
# -----------------------------------------------------------------------------
def pass_a_exact_amount(bank_records, doc_records, unmatched_vouchers, unmatched_docs):
    """
    Pass A: match vouchers to PDF docs by exact amount.
    Returns a dict voucher_number -> [file1, file2, ...] for new matches.
    """
    # Index docs by amount for quick lookup
    amt_index = {}
    for doc in doc_records:
        # Only consider PDFs (we can assume all doc_records are PDFs here)
        for amt in doc["amounts"]:
            amt_index.setdefault(amt, []).append(doc["file"])

    new_matches = {}
    for v in bank_records:
        vn = v["VoucherNumber"]
        if vn not in unmatched_vouchers:
            continue
        amt = v["Amount"]
        candidates = amt_index.get(amt, [])
        # if exactly one PDF with this amount, match it
        if len(candidates) == 1:
            new_matches[vn] = candidates
    return new_matches

def pass_b_alias_date(bank_records, doc_records, unmatched_vouchers, unmatched_docs, creditors, existing_matches):
    """
    Pass B: for remaining unmatched vouchers, narrow candidates by alias text + date window.
    Returns additional matches dict.
    """
    new_matches = {}
    # Build vendor lookup from doc_records
    for v in bank_records:
        vn = v["VoucherNumber"]
        if vn not in unmatched_vouchers:
            continue
        cred = creditors.get(v["CreditorID"])
        if not cred:
            continue
        alias_list = [(a["prefix"], a["postfix"]) for a in cred["aliases"]]
        # candidates = docs that are still unmatched
        candidates = [d for d in doc_records if d["file"] in unmatched_docs]
        matches = []
        for doc in candidates:
            # alias match
            textlines = doc["vendors"]
            if any(line.startswith(pref) and line.endswith(post) 
                   for (pref,post) in alias_list 
                   for line in textlines):
                # date proximity check (±7 days)
                vdate = v["Date_iso"]  # assume parsed date
                doc_dates = [Path(d).stem for d in doc["dates"]]  # placeholder
                # TODO: implement date parsing and window check
                matches.append(doc["file"])
        if len(matches) == 1:
            new_matches[vn] = matches
    return new_matches

def pass_c_subscription(bank_records, doc_records, unmatched_vouchers, unmatched_docs, creditors):
    """
    Pass C: enforce subscription-frequency alias entries.
    Stub: implement matching by sorting by date and matching nth voucher to nth doc.
    """
    new_matches = {}
    # TODO: implement subscription logic based on 'frequency' in alias
    return new_matches

# -----------------------------------------------------------------------------
#  I/O helpers
# -----------------------------------------------------------------------------
def load_bank(bank_csv_path):
    with open(bank_csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        records = []
        for r in reader:
            records.append({
                "VoucherNumber": int(r["VoucherNumber"]),
                "Date_iso":      r["Date"],    # "YYYY-MM-DD"
                "Amount":        float(r["Amount"].replace(',', '.')),
                "CreditorID":    int(r["CreditorID"]) if r["CreditorID"] else None,
                "Text":          r["Text"]
            })
        return records

def load_docdata(docdata_json_path):
    return json.loads(Path(docdata_json_path).read_text(encoding='utf-8'))

def load_creditors(creditors_json_path):
    data = json.loads(Path(creditors_json_path).read_text(encoding='utf-8'))
    return {c["id"]: c for c in data["creditors"]}

def load_matches(matchinfo_json_path):
    if not Path(matchinfo_json_path).exists():
        return {"matches": {}, "unmatchedVouchers": [], "unmatchedDocs": []}
    return json.loads(Path(matchinfo_json_path).read_text(encoding='utf-8'))

def save_matches(matchinfo, path):
    Path(path).write_text(json.dumps(matchinfo, indent=2), encoding='utf-8')

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
from pathlib import Path
import csv, json

# Helper functions (assume these are defined elsewhere in your module):
#   load_bank(path) -> list of dicts
#   load_docdata(path) -> list of dicts
#   load_creditors(path) -> dict of creditor_id -> creditor dict
#   load_matches(path) -> matchinfo dict
#   save_matches(matchinfo, path)

class MatcherApp(toga.App):
    def __init__(self, docs_dir, bank_kred_csv, docdata_json, matchinfo_json, creditors_json, **kwargs):
        super().__init__(formal_name="MatcherApp", app_id="com.example.matcherapp", **kwargs)

        self.docs_dir = docs_dir
        self.bank_csv = bank_kred_csv
        self.docdata_json = docdata_json
        self.matchinfo_json = matchinfo_json
        self.creditors_json = creditors_json

    def startup(self):
        # Load all data
        self.bank_records   = load_bank(self.bank_csv)
        self.doc_records    = load_docdata(self.docdata_json)
        self.creditors      = load_creditors(self.creditors_json)
        self.matchinfo      = load_matches(self.matchinfo_json)

        # Build a map for quick lookup
        self.voucher_map = {
            r["VoucherNumber"]: r for r in self.bank_records
        }

        # Compute unmatched vouchers/docs
        all_vns = sorted(self.voucher_map.keys())
        matched_vns = set(map(int, self.matchinfo["matches"].keys()))
        self.unmatched_vouchers = [vn for vn in all_vns if vn not in matched_vns]

        all_docs = {d["file"] for d in self.doc_records}
        matched_docs = {doc for docs in self.matchinfo["matches"].values() for doc in docs}
        self.unmatched_docs = sorted(all_docs - matched_docs)

        # UI: main container
        main_box = toga.Box(style=Pack(direction=COLUMN, padding=10))

        # Record info box
        info_box = toga.Box(style=Pack(direction=COLUMN, padding_bottom=10))
        self.lbl_voucher   = toga.Label('', style=Pack(padding_bottom=5))
        self.lbl_date      = toga.Label('', style=Pack(padding_bottom=5))
        self.lbl_amount    = toga.Label('', style=Pack(padding_bottom=5))
        self.lbl_creditor  = toga.Label('', style=Pack(padding_bottom=5))
        self.lbl_text      = toga.Label('', style=Pack(padding_bottom=5))
        info_box.add(self.lbl_voucher)
        info_box.add(self.lbl_date)
        info_box.add(self.lbl_amount)
        info_box.add(self.lbl_creditor)
        info_box.add(self.lbl_text)
        main_box.add(info_box)

        # Candidates table
        headings = ['File', 'Dates', 'Amounts', 'Vendors']
        accessors = ['file', 'dates', 'amounts', 'vendors']
        self.table = toga.Table(
            headings=headings,
            accessors=accessors,
            missing_value='',
            style=Pack(flex=1)
        )
        main_box.add(self.table)

        # Navigation & action buttons
        btn_box = toga.Box(style=Pack(direction=ROW, padding_top=10))
        self.btn_prev    = toga.Button('⟨ Prev', on_press=self.on_prev)
        self.btn_confirm = toga.Button('✔ Match', on_press=self.on_confirm)
        self.btn_skip    = toga.Button('✖ Skip', on_press=self.on_skip)
        self.btn_next    = toga.Button('Next ⟩', on_press=self.on_next)
        self.btn_save    = toga.Button('💾 Save & Exit', on_press=self.on_save)
        for btn in (self.btn_prev, self.btn_confirm, self.btn_skip, self.btn_next, self.btn_save):
            btn_box.add(btn)
        main_box.add(btn_box)

        # Show the window
        self.main_window = toga.MainWindow(title='Voucher Matcher')
        self.main_window.content = main_box
        self.main_window.show()

        # Start on the first record
        self.current_index = 0
        self.show_record()

    def show_record(self):
        """Display the current voucher and its candidate documents."""
        if not self.unmatched_vouchers:
            self.main_window.info_dialog("Done", "No unmatched vouchers remaining.")
            return

        vn = self.unmatched_vouchers[self.current_index]
        rec = self.voucher_map[vn]
        # Update labels
        self.lbl_voucher.text  = f"Voucher #: {vn}"
        self.lbl_date.text     = f"Date:       {rec['Date_iso']}"
        self.lbl_amount.text   = f"Amount:     {rec['Amount']}"
        cid = rec['CreditorID']
        cred = self.creditors.get(cid, {})
        cname = cred.get('name', 'Unknown')
        self.lbl_creditor.text = f"Creditor: {cname} (ID: {cid})"
        self.lbl_text.text     = f"Text:       {rec['Text']}"

        # Build candidate list by exact-amount matching
        candidates = []
        for doc in self.doc_records:
            if doc['file'] in self.unmatched_docs and rec['Amount'] in doc['amounts']:
                candidates.append({
                    'file':    doc['file'],
                    'dates':   ', '.join(doc['dates']),
                    'amounts': ', '.join(f"{a:.2f}" for a in doc['amounts']),
                    'vendors': ', '.join(doc['vendors'])
                })
        self.table.data = candidates

    def on_confirm(self, widget):
        """Assign the selected document to the current voucher."""
        selection = self.table.selection
        # no selection?
        if not selection:
            self.main_window.error_dialog("No selection", "Please select a document first.")
            return
        # selection may be a Row or a list of Row
        row = selection[0] if isinstance(selection, list) else selection
        file = row.file
        vn = self.unmatched_vouchers[self.current_index]
        self.matchinfo["matches"].setdefault(str(vn), []).append(file)
        # Remove from unmatched lists
        self.unmatched_docs.remove(file)
        self.unmatched_vouchers.pop(self.current_index)
        # Show next
        if self.current_index >= len(self.unmatched_vouchers):
            self.current_index = len(self.unmatched_vouchers) - 1
        self.show_record()

    def on_skip(self, widget):
        """Skip this voucher (leave it unmatched for now)."""
        self.unmatched_vouchers.pop(self.current_index)
        if self.current_index >= len(self.unmatched_vouchers):
            self.current_index = len(self.unmatched_vouchers) - 1
        self.show_record()

    def on_prev(self, widget):
        if self.current_index > 0:
            self.current_index -= 1
            self.show_record()

    def on_next(self, widget):
        if self.current_index < len(self.unmatched_vouchers) - 1:
            self.current_index += 1
            self.show_record()

    def on_save(self, widget):
        self.matchinfo["unmatchedVouchers"] = self.unmatched_vouchers
        self.matchinfo["unmatchedDocs"]      = self.unmatched_docs
        save_matches(self.matchinfo, self.matchinfo_json)
        self.main_window.info_dialog("Saved", "Matches written to JSON. Exiting.")
        self.main_window.close()

def show_preview(self, widget, row):
    if not row:
        return

    path = row.get('filepath')
    if not path or not os.path.exists(path):
        self.preview_scroll.content = toga.Label("File not found", style=Pack(padding=10))
        return

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".jpg", ".jpeg", ".png"):
            img = toga.Image(path)
        elif ext == ".pdf":
            from pdf2image import convert_from_path
            images = convert_from_path(path, first_page=1, last_page=1)
            if images:
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
                images[0].save(temp_file.name, format='PNG')
                img = toga.Image(temp_file.name)
            else:
                img = None
        else:
            img = None

        if img:
            self.preview_scroll.content = toga.ImageView(img)
        else:
            self.preview_scroll.content = toga.Label("Cannot preview this file type", style=Pack(padding=10))
    except Exception as e:
        print("Preview error:", e)
        self.preview_scroll.content = toga.Label("Error rendering preview", style=Pack(padding=10))

# In your main script:
def main():
    import sys
    if len(sys.argv) != 5:
        print("Usage: matcher.py bank_kred.csv docdata.json matchinfo.json creditors.json")
        sys.exit(1)
    paths = list(map(Path, sys.argv[1:5]))
    return MatcherApp('Matcher', 'org.example.matcher', paths).main_loop()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run MatcherApp for voucher-document matching")
    parser.add_argument("docs_dir", help="Directory containing the document files (PDFs/images)")
    parser.add_argument("bank_kred_csv", help="CSV file with bank transactions (creditor payments)")
    parser.add_argument("docdata_json", help="JSON file with extracted document data")
    parser.add_argument("matchinfo_json", help="JSON file to store/load confirmed matches")
    parser.add_argument("creditors_json", help="JSON file with creditor ID-to-name mapping")
    args = parser.parse_args()

    app = MatcherApp(args.docs_dir, args.bank_kred_csv, args.docdata_json, args.matchinfo_json, args.creditors_json)
    app.main_loop()
