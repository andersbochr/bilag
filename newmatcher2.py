#!/usr/bin/env python3
import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
import csv
import json
import os
import sys
from datetime import datetime, timedelta
import logging
import traceback
from pdf2image import convert_from_path
from tempfile import NamedTemporaryFile

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Set __package__ to avoid Toga initialization errors if run as a script
__package__ = "grokmatcher"


def check_display():
    """
    Ensure that a DISPLAY server is available (for Toga).
    If not, the script will fall back to “headless” mode (only file‐loading test).
    """
    logger.debug("Checking display server (DISPLAY variable)...")
    display = os.environ.get('DISPLAY')
    if not display:
        logger.warning("No display server detected (DISPLAY not set).")
        return False
    logger.info(f"Display server detected: {display}")
    return True


def validate_file(file_path: str) -> bool:
    """
    Confirm that a given filesystem path exists and is readable.
    """
    logger.debug(f"Validating file: {file_path}")
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        logger.error(f"File not found: {abs_path}")
        return False
    if not os.access(abs_path, os.R_OK):
        logger.error(f"File not readable: {abs_path}")
        return False
    logger.info(f"File validated: {abs_path}")
    return True


def parse_arguments():
    """
    Expect exactly five command‐line arguments:
      1. doc_folder       → directory containing PDFs/images
      2. bank_file        → CSV (semicolon‐delimiter) with fields:
                             VoucherNumber;Date;Amount;CreditorID;DebitAccount;CreditAccount;Text
      3. docdata_json     → JSON array produced by docprocessor.py (fields: file, dates, amounts, vendors)
      4. matchinfo_file   → (possibly nonexistent) JSON file to store “matches”
      5. creditors_file   → JSON array of creditor definitions (grokcreditors.json)
    """
    logger.debug(f"Received arguments: {sys.argv[1:]}")
    if len(sys.argv) != 6:
        logger.error(f"Usage: {sys.argv[0]} <doc_folder> <bank_file> <docdata_json> <matchinfo_file> <creditors_file>")
        sys.exit(1)

    args = {
        'doc_folder':     sys.argv[1],
        'bank_file':      sys.argv[2],
        'docdata_json':   sys.argv[3],
        'matchinfo_file': sys.argv[4],
        'creditors_file': sys.argv[5]
    }

    # Validate the files (except doc_folder, which must be a directory)
    for key, path in args.items():
        if key != 'doc_folder' and not validate_file(path):
            sys.exit(1)

    if not os.path.isdir(args['doc_folder']):
        logger.error(f"Document folder not found or not a directory: {args['doc_folder']}")
        sys.exit(1)

    return args


def load_creditors(creditors_file: str) -> dict[int, dict]:
    """
    Load the creditors JSON (array of creditor objects) and return a dict keyed by creditor ID.
    Each creditor in the JSON must look like:
      {
        "id":           123,
        "name":         "Some Vendor",
        "single_voucher": true/false,
        "aliases": [
          { "prefix": "...", "postfix": "...", "debit_account": "...", "credit_account": "...", "frequency": "...", "start_date": "YYYY-MM-DD" },
          ...
        ]
      }
    """
    logger.debug(f"Loading creditor definitions from {creditors_file}")
    try:
        with open(creditors_file, 'r', encoding='utf-8') as f:
            creditors_list = json.load(f)
        creditors_map = {int(c['id']): c for c in creditors_list}
        logger.info(f"Loaded {len(creditors_map)} creditors from {creditors_file}")
        return creditors_map

    except FileNotFoundError:
        logger.error(f"Error: {creditors_file} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing {creditors_file}: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error loading {creditors_file}: {e}\n{traceback.format_exc()}")
        sys.exit(1)


def load_bank_records(bank_file: str) -> list[dict]:
    """
    Reads the “bank” CSV (semicolon‐delimited) and returns a list of dicts:
      {
        'VoucherNumber': '0953',       # kept as string
        'Date_iso':      '2024-11-15', # normalized to ISO format
        'Amount':        1250.00,      # float
        'CreditorID':    42,           # int
        'DebitAccount':  '58000',
        'CreditAccount': '1000',
        'Text':          'Payment to Vendor A'
      }
    Any row with invalid date/amount is skipped with a warning.
    """
    logger.debug(f"Loading bank records from {bank_file}")
    try:
        bank_records: list[dict] = []
        with open(bank_file, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile, delimiter=';')
            for row in reader:
                date_str = row.get('Date', '')
                # Attempt to parse date into ISO format YYYY-MM-DD
                try:
                    if '-' in date_str:
                        day, month, year = date_str.split('-')
                    elif '.' in date_str:
                        day, month, year = date_str.split('.')
                    else:
                        raise ValueError(f"Unknown date format: {date_str}")

                    if len(year) == 2:
                        year = f"20{year}"
                    iso_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                    datetime.fromisoformat(iso_date)  # validate
                except (ValueError, IndexError):
                    logger.warning(f"Skipping row with invalid date: {date_str}")
                    continue

                # Parse amount into float
                try:
                    amt = row['Amount'].replace(' ', '')
                    if ',' in amt and '.' in amt:
                        # Assume Danish format ('.' as thousands, ',' as decimal)
                        amt = amt.replace('.', '').replace(',', '.')
                    elif ',' in amt:
                        amt = amt.replace(',', '.')
                    amount = float(amt)
                except (ValueError, KeyError):
                    logger.warning(f"Skipping row with invalid amount: {row.get('Amount', '')}")
                    continue

                # CreditorID might be empty string → treat as 0 or skip? Here, skip if missing.
                try:
                    creditor_id = int(row['CreditorID'])
                except (ValueError, KeyError):
                    logger.warning(f"Skipping row with invalid CreditorID: {row.get('CreditorID', '')}")
                    continue

                bank_records.append({
                    'VoucherNumber': row['VoucherNumber'],
                    'Date_iso':      iso_date,
                    'Amount':        amount,
                    'CreditorID':    creditor_id,
                    'DebitAccount':  row.get('DebitAccount', '').strip(),
                    'CreditAccount': row.get('CreditAccount', '').strip(),
                    'Text':          row.get('Text', '').strip()
                })

        if not bank_records:
            logger.error(f"No valid records found in {bank_file}; exiting.")
            sys.exit(1)

        logger.info(f"Loaded {len(bank_records)} bank records")
        return bank_records

    except FileNotFoundError:
        logger.error(f"Bank file not found: {bank_file}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading bank file: {e}\n{traceback.format_exc()}")
        sys.exit(1)


def load_doc_records(docdata_json: str) -> list[dict]:
    """
    Reads the JSON array from docdata_json (produced by docprocessor.py), where each element is:
      {
        "file":    "invoice123.pdf",
        "dates":   ["2024-11-01", "2024-11-05", ...],
        "amounts": [1250.0,  250.0, ...],
        "vendors": ["VENDOR A", "VENDOR B", ...]
      }
    Returns the list of dicts. Any malformed entries cause an exit.
    """
    logger.debug(f"Loading document records from {docdata_json}")
    try:
        with open(docdata_json, 'r', encoding='utf-8') as f:
            docs = json.load(f)

        if not isinstance(docs, list) or not docs:
            logger.error(f"{docdata_json} is empty or not an array; exiting.")
            sys.exit(1)

        # Ensure amounts are floats, dates remain strings, vendors remain lists
        for doc in docs:
            # Normalize amounts (some might be strings)
            clean_amounts: list[float] = []
            for a in doc.get('amounts', []):
                try:
                    clean_amounts.append(float(a))
                except Exception:
                    pass
            doc['amounts'] = clean_amounts
            doc['dates']   = [d for d in doc.get('dates', []) if isinstance(d, str)]
            doc['vendors'] = [v for v in doc.get('vendors', []) if isinstance(v, str)]

        logger.info(f"Loaded {len(docs)} document records")
        return docs

    except FileNotFoundError:
        logger.error(f"Document JSON file not found: {docdata_json}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"{docdata_json} contains invalid JSON: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading {docdata_json}: {e}\n{traceback.format_exc()}")
        sys.exit(1)


def load_matchinfo(matchinfo_file: str) -> dict:
    """
    Loads an existing matchinfo JSON. The new format only contains:
      { "matches": { "<VoucherNumber>": ["file1.pdf", ...], ... } }
    If the file does not exist, return { "matches": {} }.
    If it is malformed, issue a warning and fallback to an empty matches map.
    """
    logger.debug(f"Loading matchinfo from {matchinfo_file}")
    try:
        if not os.path.exists(matchinfo_file):
            logger.warning(f"Matchinfo file not found: {matchinfo_file}. Initializing empty.")
            return {'matches': {}}

        with open(matchinfo_file, 'r', encoding='utf-8') as f:
            loaded = json.load(f)

        # Only keep the "matches" key; ignore anything else if present
        raw_matches = loaded.get('matches', {})
        if not isinstance(raw_matches, dict):
            logger.warning(f"Ignoring invalid 'matches' in {matchinfo_file}. Reinitializing to empty.")
            raw_matches = {}

        # Ensure all keys are strings and all values are lists of strings
        clean_matches: dict[str, list[str]] = {}
        for k, v in raw_matches.items():
            if not isinstance(k, str):
                logger.warning(f"Skipping invalid voucher key (not a string): {k}")
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                logger.warning(f"Skipping invalid match list for voucher {k}")
                continue
            clean_matches[k] = v

        logger.info(f"Loaded matchinfo: {len(clean_matches)} existing matches")
        return {'matches': clean_matches}

    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading {matchinfo_file}: {e}\n{traceback.format_exc()}")
        logger.warning("Reinitializing matchinfo to empty.")
        return {'matches': {}}


def save_matchinfo(matchinfo_file: str, matchinfo: dict):
    """
    Overwrites matchinfo_file with:
      { "matches": { ... } }
    No more "unmatchedVouchers" or "unmatchedDocs".
    """
    logger.debug(f"Saving matchinfo to {matchinfo_file}")
    try:
        # Only write out the "matches" key
        out = {'matches': matchinfo.get('matches', {})}
        with open(matchinfo_file, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        logger.info(f"Wrote matchinfo ({len(out['matches'])} matches) to {matchinfo_file}")
    except IOError as e:
        logger.error(f"Error saving matchinfo to {matchinfo_file}: {e}\n{traceback.format_exc()}")


# -----------------------------------------------------------------------------
#  PASS A: Exact Amount Matching
# -----------------------------------------------------------------------------
def pass_a_exact_amount(
    bank_records: list[dict],
    doc_records:  list[dict],
    unmatched_vouchers: list[str],
    unmatched_docs: list[str]
) -> dict[str, list[str]]:
    """
    Returns a dict of { voucherNumber → [list of doc filenames that have exactly this amount] }
    but only for vouchers in unmatched_vouchers.
    This pass DOES NOT assign a match unless there's exactly one candidate document.
    """
    logger.debug("Running Pass A: exact‐amount matching")
    matches: dict[str, list[str]] = {}

    # Build an index: amount → [docs that contain that amount]
    doc_by_amount: dict[float, list[str]] = {}
    for doc in doc_records:
        for amt in doc.get('amounts', []):
            key = round(float(amt), 2)
            doc_by_amount.setdefault(key, []).append(doc['file'])

    for voucher in bank_records:
        vn = voucher['VoucherNumber']
        if vn not in unmatched_vouchers:
            continue
        amt_key = round(float(voucher['Amount']), 2)
        candidates = doc_by_amount.get(amt_key, [])
        if len(candidates) == 1:
            # Only one document has exactly this amount → auto‐match
            matches[vn] = candidates.copy()

    logger.info(f"Pass A found {len(matches)} unique exact‐amount matches")
    return matches


# -----------------------------------------------------------------------------
#  PASS B: Alias + Date Window Matching
# -----------------------------------------------------------------------------
def pass_b_alias_date(
    bank_records:       list[dict],
    doc_records:        list[dict],
    unmatched_vouchers: list[str],
    unmatched_docs:     list[str],
    creditors:          dict[int, dict]
) -> dict[str, list[str]]:
    """
    For each voucher in unmatched_vouchers:
      1) Look up creditor = creditors[voucher['CreditorID']]
      2) For each alias in creditor['aliases'], check if any vendor‐line in the doc startswith(prefix) and endswith(postfix).
      3) If alias matches, find the document's closest date vs. voucher date; if within ±15 days, include it as a candidate.
      4) Only auto‐match if exactly one candidate emerges.
    """
    logger.debug("Running Pass B: alias + date‐window matching")
    new_matches: dict[str, list[str]] = {}

    for v in bank_records:
        vn = v['VoucherNumber']
        if vn not in unmatched_vouchers:
            continue

        cred_id = v.get('CreditorID')
        if cred_id not in creditors:
            continue
        cred = creditors[cred_id]

        # Build list of (prefix, postfix) pairs
        alias_list: list[tuple[str, str]] = []
        for a in cred.get('aliases', []):
            pref = a.get('prefix', '')
            post = a.get('postfix', '')
            alias_list.append((pref, post))

        try:
            v_date = datetime.fromisoformat(v['Date_iso'])
        except ValueError:
            logger.warning(f"Skipping voucher {vn} due to invalid date: {v['Date_iso']}")
            continue

        # Consider only documents that are still unmatched
        candidates = []
        for doc in doc_records:
            if doc['file'] not in unmatched_docs:
                continue

            # Check if any vendor line matches any alias (startswith + endswith)
            vendor_lines = doc.get('vendors', [])
            matched_alias = False
            for line in vendor_lines:
                for (pref, post) in alias_list:
                    if line.startswith(pref) and (not post or line.endswith(post)):
                        matched_alias = True
                        break
                if matched_alias:
                    break

            if not matched_alias:
                continue

            # If alias matched, check date proximity
            doc_dates = []
            for d in doc.get('dates', []):
                try:
                    doc_dates.append(datetime.fromisoformat(d))
                except Exception:
                    pass

            if doc_dates:
                # Compare the closest date in the document vs. v_date
                closest = min(doc_dates, key=lambda dd: abs((dd - v_date).days))
                if abs((closest - v_date).days) <= 15:
                    candidates.append(doc['file'])
            else:
                # No dates in doc → still include as candidate
                candidates.append(doc['file'])

        if len(candidates) == 1:
            new_matches[vn] = candidates.copy()

    logger.info(f"Pass B found {len(new_matches)} unique alias‐date matches")
    return new_matches


# -----------------------------------------------------------------------------
#  PASS C: Subscription‐Frequency Matching
# -----------------------------------------------------------------------------
def pass_c_subscription(
    bank_records:       list[dict],
    doc_records:        list[dict],
    unmatched_vouchers: list[str],
    unmatched_docs:     list[str],
    creditors:          dict[int, dict]
) -> dict[str, list[str]]:
    """
    For each voucher in unmatched_vouchers whose creditor has an alias with a "frequency" field:
      1) That alias has a 'start_date' and 'frequency' (e.g. "monthly", "quarterly", "semi-annual", "bimonthly").
      2) Build a list of expected subscription dates from start_date up through voucher date + one cycle.
      3) For each unmatched document with matching amount and alias text, compute doc's closest date vs. voucher date.
         If that date is within +/- 7 days of any expected date, it's a candidate.
      4) Auto‐match only if exactly one candidate emerges.
    """
    logger.debug("Running Pass C: subscription frequency matching")
    frequency_deltas = {
        'monthly':     timedelta(days=30),
        'quarterly':   timedelta(days=90),
        'semi-annual': timedelta(days=180),
        'bimonthly':   timedelta(days=60)
    }

    new_matches: dict[str, list[str]] = {}

    for v in bank_records:
        vn = v['VoucherNumber']
        if vn not in unmatched_vouchers:
            continue

        cred_id = v.get('CreditorID')
        if cred_id not in creditors:
            continue
        cred = creditors[cred_id]

        # Filter aliases that have a 'frequency' and 'start_date'
        subscription_aliases = []
        for a in cred.get('aliases', []):
            freq = a.get('frequency')
            start = a.get('start_date')
            if freq and start:
                subscription_aliases.append(a)

        if not subscription_aliases:
            continue

        try:
            v_date = datetime.fromisoformat(v['Date_iso'])
        except ValueError:
            logger.warning(f"Skipping voucher {vn} due to invalid date: {v['Date_iso']}")
            continue

        v_amount = float(v['Amount'])
        candidates = []

        for doc in doc_records:
            if doc['file'] not in unmatched_docs:
                continue

            # Quick filter: amount must appear in doc
            if round(v_amount, 2) not in [round(float(x), 2) for x in doc.get('amounts', [])]:
                continue

            # Check vendor‐line alias match for at least one subscription_alias
            vendor_lines = doc.get('vendors', [])
            alias_matched = False
            for a in subscription_aliases:
                pref = a.get('prefix', '')
                post = a.get('postfix', '')
                for line in vendor_lines:
                    if line.startswith(pref) and (not post or line.endswith(post)):
                        alias_matched = True
                        break
                if alias_matched:
                    break
            if not alias_matched:
                continue

            # Collect all document dates for proximity checks
            doc_dates = []
            for d in doc.get('dates', []):
                try:
                    doc_dates.append(datetime.fromisoformat(d))
                except Exception:
                    pass

            # Now, for each alias with frequency & start_date, generate expected schedule
            for a in subscription_aliases:
                freq = a.get('frequency')
                start_str = a.get('start_date')
                if not freq or not start_str:
                    continue

                try:
                    start_date = datetime.fromisoformat(start_str)
                except ValueError:
                    logger.warning(f"Invalid start_date {start_str} for creditor {cred.get('name','?')}")
                    continue

                delta = frequency_deltas.get(freq, timedelta(days=30))
                expected_dates: list[datetime] = []
                current = start_date
                # Build expected dates from start_date up to (voucher_date + one cycle)
                while current <= (v_date + delta):
                    if current >= start_date:
                        expected_dates.append(current)
                    current = current + delta

                if doc_dates:
                    closest_doc_date = min(doc_dates, key=lambda dd: abs((dd - v_date).days))
                    for exp in expected_dates:
                        if abs((closest_doc_date - exp).days) <= 7:
                            candidates.append(doc['file'])
                            break
                else:
                    # If doc has no dates, but the amount & alias matched, we can consider it a candidate
                    candidates.append(doc['file'])

        # If exactly one candidate emerges, auto‐match
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) == 1:
            new_matches[vn] = [unique_candidates[0]]

    logger.info(f"Pass C found {len(new_matches)} subscription‐frequency matches")
    return new_matches


def test_file_loading(bank_file: str, docdata_json: str, creditors_file: str, matchinfo_file: str) -> bool:
    """
    In headless mode (no DISPLAY), simply verify that we can load all required files.
    Exits with sys.exit(1) if any of them fail.
    """
    logger.info("Running headless file‐loading test")
    try:
        bank_records  = load_bank_records(bank_file)
        doc_records   = load_doc_records(docdata_json)
        creditors     = load_creditors(creditors_file)
        matchinfo     = load_matchinfo(matchinfo_file)
        _ = matchinfo.get('matches', {})
        logger.info("All files loaded successfully.")
        return True
    except Exception as e:
        logger.error(f"File loading test failed: {e}\n{traceback.format_exc()}")
        return False

#!/usr/bin/env python3
import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
import csv
import json
import os
import sys
from datetime import datetime, timedelta
import logging
import traceback
from pdf2image import convert_from_path
from tempfile import NamedTemporaryFile

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Set __package__ to avoid Toga initialization errors if run as a script
__package__ = "grokmatcher"

# [Previous functions unchanged: check_display, validate_file, parse_arguments, load_creditors, load_bank_records, load_doc_records, load_matchinfo, save_matchinfo, pass_a_exact_amount, pass_b_alias_date, pass_c_subscription, test_file_loading]

class GrokMatcher(toga.App):
    def __init__(self, doc_folder, bank_file, docdata_json, matchinfo_file, creditors_file):
        logger.debug("Initializing GrokMatcher")
        try:
            super().__init__('GrokMatcher', 'org.example.grokmatcher')
            self.doc_folder      = doc_folder
            self.bank_file       = bank_file
            self.docdata_json    = docdata_json
            self.matchinfo_file  = matchinfo_file
            self.creditors_file  = creditors_file
            self.matchinfo = load_matchinfo(self.matchinfo_file)
            self.on_exit = self.on_exit
            logger.info("GrokMatcher initialized successfully.")
        except Exception as e:
            logger.error(f"Error initializing GrokMatcher: {e}\n{traceback.format_exc()}")
            sys.exit(1)

    def startup(self):
        logger.debug("Entering startup()")
        self.bank_records = load_bank_records(self.bank_file)
        self.doc_records  = load_doc_records(self.docdata_json)
        self.creditors    = load_creditors(self.creditors_file)
        self.voucher_map = {r['VoucherNumber']: r for r in self.bank_records}

        all_vouchers = set(self.voucher_map.keys())
        matched_vouchers = set(self.matchinfo.get('matches', {}).keys())
        self.unmatched_vouchers = sorted(
            list(all_vouchers - matched_vouchers),
            key=lambda x: int(x)
        )

        all_docs = set(doc['file'] for doc in self.doc_records)
        matched_docs = set()
        for docs_list in self.matchinfo.get('matches', {}).values():
            for fn in docs_list:
                matched_docs.add(fn)
        self.unmatched_docs = sorted(all_docs - matched_docs)

        logger.info(f"Unmatched vouchers at startup: {len(self.unmatched_vouchers)}")
        logger.info(f"Unmatched docs at startup:     {len(self.unmatched_docs)}")

        new_a = pass_a_exact_amount(
            self.bank_records, self.doc_records, self.unmatched_vouchers, self.unmatched_docs
        )
        if new_a:
            for vn, doc_list in new_a.items():
                if vn not in self.matchinfo['matches']:
                    self.matchinfo['matches'][vn] = doc_list.copy()
                    if vn in self.unmatched_vouchers:
                        self.unmatched_vouchers.remove(vn)
                    for dd in doc_list:
                        if dd in self.unmatched_docs:
                            self.unmatched_docs.remove(dd)

        new_b = pass_b_alias_date(
            self.bank_records, self.doc_records, self.unmatched_vouchers, self.unmatched_docs, self.creditors
        )
        if new_b:
            for vn, doc_list in new_b.items():
                if vn not in self.matchinfo['matches']:
                    self.matchinfo['matches'][vn] = doc_list.copy()
                    if vn in self.unmatched_vouchers:
                        self.unmatched_vouchers.remove(vn)
                    for dd in doc_list:
                        if dd in self.unmatched_docs:
                            self.unmatched_docs.remove(dd)

        new_c = pass_c_subscription(
            self.bank_records, self.doc_records, self.unmatched_vouchers, self.unmatched_docs, self.creditors
        )
        if new_c:
            for vn, doc_list in new_c.items():
                if vn not in self.matchinfo['matches']:
                    self.matchinfo['matches'][vn] = doc_list.copy()
                    if vn in self.unmatched_vouchers:
                        self.unmatched_vouchers.remove(vn)
                    for dd in doc_list:
                        if dd in self.unmatched_docs:
                            self.unmatched_docs.remove(dd)

        if new_a or new_b or new_c:
            logger.info("Automatic passes produced new matches; saving updated matchinfo.json")
            save_matchinfo(self.matchinfo_file, self.matchinfo)

        if not check_display():
            logger.warning("Headless mode detected; stopping after file‐loading test.")
            if not test_file_loading(
                self.bank_file, self.docdata_json, self.creditors_file, self.matchinfo_file
            ):
                logger.error("File loading test failed; exiting.")
                sys.exit(1)
            logger.info("File loading test succeeded. Exiting (GUI is required to proceed).")
            sys.exit(0)

        self.current_index = 0
        self.current_pdf_file = None
        self.current_pdf_images = []
        self.current_pdf_page = 0

        self.main_window = toga.MainWindow(title=self.formal_name)

        left_container = toga.Box(style=Pack(direction=COLUMN, margin=10))
        self.lbl_voucher  = toga.Label('', style=Pack(margin=(0, 0, 5, 0), font_size=14))
        self.lbl_date     = toga.Label('', style=Pack(margin=(0, 0, 5, 0)))
        self.lbl_amount   = toga.Label('', style=Pack(margin=(0, 0, 5, 0)))
        self.lbl_creditor = toga.Label('', style=Pack(margin=(0, 0, 5, 0)))
        self.lbl_text     = toga.Label('', style=Pack(margin=(0, 0, 5, 0)))

        left_container.add(self.lbl_voucher)
        left_container.add(self.lbl_date)
        left_container.add(self.lbl_amount)
        left_container.add(self.lbl_creditor)
        left_container.add(self.lbl_text)

        controls_box = toga.Box(style=Pack(direction=ROW, margin=(0, 10, 10, 10)))
        self.switch_all_docs = toga.Switch(
            "Show All Documents",
            style=Pack(margin=(0, 10, 0, 0))
        )
        self.switch_all_docs.on_toggle = self.refresh_table
        self.switch_all_docs.value = False

        self.switch_hide_matched = toga.Switch(
            "Hide Matched Docs",
            style=Pack(margin=(0, 0, 0, 0))
        )
        self.switch_hide_matched.on_toggle = self.refresh_table
        self.switch_hide_matched.value = True

        controls_box.add(self.switch_all_docs)
        controls_box.add(self.switch_hide_matched)

        self.table = toga.Table(
            headings=['File', 'Dates', 'Amounts', 'Vendors', 'Score'],
            accessors=['file', 'dates', 'amounts', 'vendors', 'score'],
            missing_value='',
            style=Pack(flex=1, margin=10),
            on_select=self.show_document_preview
        )

        self.preview_scroll = toga.ScrollContainer(style=Pack(flex=1, margin=10))
        self.preview_scroll.content = toga.Label(
            'Select a document to preview',
            style=Pack(margin=10)
        )

        pdf_nav_box = toga.Box(style=Pack(direction=ROW, margin=5))
        self.btn_pdf_prev = toga.Button(
            "« Prev Page",
            on_press=self.prev_pdf_page,
            style=Pack(margin=5, width=100)
        )
        self.btn_pdf_next = toga.Button(
            "Next Page »",
            on_press=self.next_pdf_page,
            style=Pack(margin=5, width=100)
        )
        pdf_nav_box.add(self.btn_pdf_prev)
        pdf_nav_box.add(self.btn_pdf_next)

        button_box = toga.Box(style=Pack(direction=ROW, margin=10))
        self.btn_prev  = toga.Button('← Previous Voucher', on_press=self.prev_record, style=Pack(margin=5, width=150))
        self.btn_next  = toga.Button('Next Voucher →',     on_press=self.next_record, style=Pack(margin=5, width=150))
        self.btn_match = toga.Button('Match This Voucher', on_press=self.match_record, style=Pack(margin=5, width=150))
        self.btn_save  = toga.Button('Save & Exit',         on_press=self.save_and_exit, style=Pack(margin=5, width=120))

        button_box.add(self.btn_prev)
        button_box.add(self.btn_next)
        button_box.add(self.btn_match)
        button_box.add(self.btn_save)

        right_container = toga.Box(style=Pack(direction=COLUMN, flex=1))
        right_container.add(controls_box)
        right_container.add(self.table)
        right_container.add(self.preview_scroll)
        right_container.add(pdf_nav_box)

        split_box = toga.Box(style=Pack(direction=ROW, margin=0, flex=1))
        split_box.add(left_container)
        split_box.add(right_container)

        main_box = toga.Box(style=Pack(direction=COLUMN, flex=1))
        main_box.add(split_box)
        main_box.add(button_box)

        self.main_window.content = main_box
        self.main_window.show()

        if self.unmatched_vouchers:
            self.show_record()
        else:
            self.main_window.info_dialog("All Matched", "There are no unmatched vouchers remaining.")
            self.main_window.close()

    def show_record(self):
        if not self.unmatched_vouchers:
            return
        try:
            vn = self.unmatched_vouchers[self.current_index]
            voucher = self.voucher_map[vn]
            self.lbl_voucher.text  = f"Voucher #: {vn}"
            self.lbl_date.text     = f"Date: {voucher['Date_iso']}"
            self.lbl_amount.text   = f"Amount: {voucher['Amount']:.2f}"
            self.lbl_creditor.text = f"Creditor ID: {voucher['CreditorID']}"
            self.lbl_text.text     = f"Text: {voucher.get('Text', '')}"
            self.preview_scroll.content = toga.Label('Select a document to preview', style=Pack(margin=10))
            self.current_pdf_images = []
            self.current_pdf_file = None
            self.current_pdf_page = 0
            self.refresh_table()
            logger.info(f"Displayed voucher {vn} with updated candidate/all-doc list")
        except Exception as e:
            logger.error(f"Error in show_record(): {e}\n{traceback.format_exc()}")
            sys.exit(1)


    def refresh_table(self, widget=None, **kwargs):
        logger.debug(
            f"refresh_table called: switch_all_docs={self.switch_all_docs.value}  "
            f"switch_hide_matched={self.switch_hide_matched.value}"
        )
        if not self.unmatched_vouchers:
            self.table.data = []
            logger.debug("No unmatched vouchers; clearing table")
            return

        try:
            vn = self.unmatched_vouchers[self.current_index]
            voucher = self.voucher_map[vn]
            v_date = datetime.fromisoformat(voucher['Date_iso'])
            v_amount = float(voucher['Amount'])
            cred_id = voucher['CreditorID']

            matched_docs = set()
            for docs_list in self.matchinfo.get('matches', {}).values():
                for fn in docs_list:
                    matched_docs.add(fn)

            rows = []
            for doc in self.doc_records:
                filename = doc['file']

                if self.switch_hide_matched.value and filename in matched_docs:
                    logger.debug(f"Skipping matched document: {filename}")
                    continue

                score = 0
                if round(v_amount, 2) in [round(float(x), 2) for x in doc.get('amounts', [])]:
                    score += 50

                alias_bonus = 0
                if cred_id in self.creditors:
                    cred = self.creditors[cred_id]
                    for a in cred.get('aliases', []):
                        pref = a.get('prefix', '')
                        post = a.get('postfix', '')
                        for line in doc.get('vendors', []):
                            if line.startswith(pref) and (not post or line.endswith(post)):
                                alias_bonus = 30
                                break
                        if alias_bonus:
                            break
                score += alias_bonus

                date_bonus = 0
                doc_dates = []
                for d in doc.get('dates', []):
                    try:
                        doc_dates.append(datetime.fromisoformat(d))
                    except Exception:
                        pass
                if doc_dates:
                    closest = min(doc_dates, key=lambda dd: abs((dd - v_date).days))
                    diff_days = abs((closest - v_date).days)
                    if diff_days <= 7:
                        date_bonus = 20
                    elif diff_days <= 15:
                        date_bonus = 10
                    elif diff_days <= 30:
                        date_bonus = 5
                score += date_bonus

                if not self.switch_all_docs.value and score == 0:
                    logger.debug(f"Skipping non-candidate document (score=0): {filename}")
                    continue

                unique_dates = sorted(set(doc.get('dates', [])))
                dates_str = ', '.join(unique_dates[:5])

                unique_amounts = sorted({round(float(x), 2) for x in doc.get('amounts', [])})
                amounts_str = ', '.join(f"{amt:.2f}" for amt in unique_amounts[:5])

                seen = set()
                unique_vendors = []
                for vline in doc.get('vendors', []):
                    if vline not in seen:
                        seen.add(vline)
                        unique_vendors.append(vline)
                    if len(unique_vendors) >= 5:
                        break
                vendors_str = ', '.join(unique_vendors)

                rows.append({
                    'file':    filename,
                    'dates':   dates_str,
                    'amounts': amounts_str,
                    'vendors': vendors_str,
                    'score':   str(score)
                })

            rows.sort(key=lambda r: (-int(r['score']), r['file']))
            logger.debug(f"Populating table with {len(rows)} rows (all_docs={self.switch_all_docs.value}, hide_matched={self.switch_hide_matched.value})")
            
            # Set the new data
            self.table.data = rows
            self.table._impl.native.get_selection().unselect_all()

        except Exception as e:
            logger.error(f"Error in refresh_table(): {e}\n{traceback.format_exc()}")

    def show_document_preview(self, widget):
        row = self.table.selection
        if row is None:
            self.preview_scroll.content = toga.Label("No document selected", style=Pack(margin=10))
            return

        file_path = os.path.join(self.doc_folder, row.file)
        if not os.path.exists(file_path):
            self.preview_scroll.content = toga.Label("File not found", style=Pack(margin=10))
            return

        try:
            if file_path.lower().endswith('.pdf'):
                self.current_pdf_images = convert_from_path(file_path, dpi=200)
                self.current_pdf_file = file_path
                self.current_pdf_page = 0
                self.show_pdf_page()
            else:
                img = toga.Image(file_path)
                iv = toga.ImageView(img, style=Pack(width=400, height=600))
                self.preview_scroll.content = iv
            logger.debug(f"Previewed document: {row.file}")
        except Exception as e:
            logger.error(f"Error previewing {file_path}: {e}")
            self.preview_scroll.content = toga.Label("Failed to preview file", style=Pack(margin=10))

    def show_pdf_page(self):
        if not self.current_pdf_images:
            return
        try:
            page_img = self.current_pdf_images[self.current_pdf_page]
            with NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                page_img.save(tmp.name, format='PNG')
                img = toga.Image(tmp.name)
                iv = toga.ImageView(img, style=Pack(width=400, height=600))
                self.preview_scroll.content = iv
            logger.debug(f"Displayed PDF page {self.current_pdf_page + 1} of {self.current_pdf_file}")
        except Exception as e:
            logger.error(f"Error rendering PDF page: {e}")
            self.preview_scroll.content = toga.Label("Error rendering PDF page", style=Pack(margin=10))

    def next_pdf_page(self, widget):
        if self.current_pdf_images and self.current_pdf_page < len(self.current_pdf_images) - 1:
            self.current_pdf_page += 1
            self.show_pdf_page()

    def prev_pdf_page(self, widget):
        if self.current_pdf_images and self.current_pdf_page > 0:
            self.current_pdf_page -= 1
            self.show_pdf_page()

    def prev_record(self, widget):
        if self.current_index > 0:
            self.current_index -= 1
            self.show_record()

    def next_record(self, widget):
        if self.current_index < len(self.unmatched_vouchers) - 1:
            self.current_index += 1
            self.show_record()

    def match_record(self, widget):
        if not self.table.selection:
            self.main_window.info_dialog('Error', 'No document selected.')
            return

        vn = self.unmatched_vouchers[self.current_index]
        doc_file = self.table.selection.file

        if vn not in self.matchinfo['matches']:
            self.matchinfo['matches'][vn] = [doc_file]
        else:
            if doc_file not in self.matchinfo['matches'][vn]:
                self.matchinfo['matches'][vn].append(doc_file)

        if vn in self.unmatched_vouchers:
            self.unmatched_vouchers.remove(vn)

        logger.info(f"Manually matched voucher {vn} → {doc_file}")
        save_matchinfo(self.matchinfo_file, self.matchinfo)

        if self.current_index >= len(self.unmatched_vouchers):
            self.current_index = max(0, len(self.unmatched_vouchers) - 1)

        if self.unmatched_vouchers:
            self.show_record()
        else:
            self.main_window.info_dialog("All Done", "All vouchers have now been matched.")
            self.main_window.close()

    def save_and_exit(self, widget):
        save_matchinfo(self.matchinfo_file, self.matchinfo)
        self.main_window.info_dialog("Saved", "Match information saved.")
        self.main_window.close()

    def on_exit(self):
        save_matchinfo(self.matchinfo_file, self.matchinfo)
        return True

def main():
    logger.debug("Entering main()")
    try:
        args = parse_arguments()
        for file_key in ['bank_file', 'docdata_json', 'matchinfo_file', 'creditors_file']:
            out_dir = os.path.dirname(args[file_key])
            if out_dir and not os.path.exists(out_dir):
                logger.info(f"Creating directory: {out_dir}")
                os.makedirs(out_dir)

        if not check_display():
            logger.warning("Headless mode: will only test file loading.")
            if not test_file_loading(
                args['bank_file'], args['docdata_json'], args['creditors_file'], args['matchinfo_file']
            ):
                logger.error("File loading test failed; exiting.")
                sys.exit(1)
            logger.info("File loading test succeeded. Exiting (GUI is required to proceed).")
            sys.exit(0)

        return GrokMatcher(
            args['doc_folder'], args['bank_file'], args['docdata_json'], args['matchinfo_file'], args['creditors_file']
        )

    except Exception as e:
        logger.error(f"Error initializing application: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == '__main__':
    app = main()
    if app:
        app.main_loop()