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
    If not, the script will fall back to “headless” mode (which only tests file loading).
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
        creditors_map = { int(c['id']): c for c in creditors_list }
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
      { "matches": { "<VoucherNumber>": ["file1.pdf", "file2.jpg", ...], ... } }
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
        # Just ensure we got a 'matches' dict
        _ = matchinfo.get('matches', {})
        logger.info("All files loaded successfully.")
        return True
    except Exception as e:
        logger.error(f"File loading test failed: {e}\n{traceback.format_exc()}")
        return False


class GrokMatcher(toga.App):
    """
    Toga‐based application that allows you to step through unmatched vouchers one by one,
    see candidate documents side by side, and “Match” or “Skip” each voucher.
    The only state saved on disk is matchinfo.json (with a single 'matches' object).
    """
    def __init__(self, doc_folder, bank_file, docdata_json, matchinfo_file, creditors_file):
        logger.debug("Initializing GrokMatcher")
        try:
            super().__init__('GrokMatcher', 'org.example.grokmatcher')
            self.doc_folder      = doc_folder
            self.bank_file       = bank_file
            self.docdata_json    = docdata_json
            self.matchinfo_file  = matchinfo_file
            self.creditors_file  = creditors_file

            # Load or initialize matchinfo (only “matches” map)
            self.matchinfo = load_matchinfo(self.matchinfo_file)

            # (We do NOT store unmatched lists in matchinfo.json anymore.)

            # Hook on_exit so we save on close
            self.on_exit = self.on_exit

            logger.info("GrokMatcher initialized successfully.")
        except Exception as e:
            logger.error(f"Error initializing GrokMatcher: {e}\n{traceback.format_exc()}")
            sys.exit(1)

    def startup(self):
        """
        1) Read bank_records, doc_records, creditors.
        2) Build voucher_map (VoucherNumber → bank_record dict).
        3) Compute unmatched_vouchers = all vouchers not in matchinfo['matches'].
        4) Compute unmatched_docs     = all doc filenames not already matched to any voucher.
        5) Run three automatic passes (A, B, C). Merge their results into matchinfo['matches'],
           update unmatched lists, and save matchinfo.json if any new auto‐matches occurred.
        6) Build UI and show the first unmatched voucher (if any remain).
        """
        logger.debug("Entering startup()")

        # 1) Load all data sources
        logger.debug("Loading bank records...")
        self.bank_records = load_bank_records(self.bank_file)
        logger.debug("Loading document records...")
        self.doc_records  = load_doc_records(self.docdata_json)
        logger.debug("Loading creditors...")
        self.creditors    = load_creditors(self.creditors_file)

        # 2) Build a quick lookup map: VoucherNumber → bank_record
        self.voucher_map = { r['VoucherNumber']: r for r in self.bank_records }

        # 3) Compute unmatched_vouchers (strings) and unmatched_docs
        all_vouchers = set(self.voucher_map.keys())                           # set[str]
        matched_vouchers = set(self.matchinfo.get('matches', {}).keys())       # set[str]
        self.unmatched_vouchers = sorted(all_vouchers - matched_vouchers)

        all_docs = set(doc['file'] for doc in self.doc_records)                # set[str]
        matched_docs = set()
        for docs_list in self.matchinfo.get('matches', {}).values():
            for fn in docs_list:
                matched_docs.add(fn)
        self.unmatched_docs = sorted(all_docs - matched_docs)

        logger.info(f"Unmatched vouchers at startup: {len(self.unmatched_vouchers)}")
        logger.info(f"Unmatched docs at startup:     {len(self.unmatched_docs)}")

        # 4) Run the three automatic passes, in sequence. Any new auto‐matches get added below.

        # PASS A
        new_a = pass_a_exact_amount(
            self.bank_records,
            self.doc_records,
            self.unmatched_vouchers,
            self.unmatched_docs
        )
        # Add to matchinfo, remove from unmatched lists
        if new_a:
            for vn, doc_list in new_a.items():
                if vn not in self.matchinfo['matches']:
                    self.matchinfo['matches'][vn] = doc_list.copy()
                    if vn in self.unmatched_vouchers:
                        self.unmatched_vouchers.remove(vn)
                    for dd in doc_list:
                        if dd in self.unmatched_docs:
                            self.unmatched_docs.remove(dd)

        # PASS B
        new_b = pass_b_alias_date(
            self.bank_records,
            self.doc_records,
            self.unmatched_vouchers,
            self.unmatched_docs,
            self.creditors
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

        # PASS C
        new_c = pass_c_subscription(
            self.bank_records,
            self.doc_records,
            self.unmatched_vouchers,
            self.unmatched_docs,
            self.creditors
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

        # If any of the three passes added new matches, save matchinfo.json now.
        if new_a or new_b or new_c:
            logger.info("Automatic passes produced new matches; saving updated matchinfo.json")
            save_matchinfo(self.matchinfo_file, self.matchinfo)

        # 5) Build the GUI (only if we still have unmatched vouchers)
        if not check_display():
            logger.warning("Headless mode detected; stopping after file‐loading test.")
            sys.exit(0)

        # Create main window
        self.current_index = 0
        self.current_pdf_file = None
        self.current_pdf_images = []
        self.current_pdf_page = 0

        self.main_window = toga.MainWindow(title=self.formal_name)

        # Left panel: voucher info (VoucherNumber, Date, Amount, CreditorID, Text)
        left_container = toga.Box(style=Pack(direction=COLUMN, padding=10))
        self.lbl_voucher  = toga.Label('', style=Pack(padding=(0, 0, 5, 0)))
        self.lbl_date     = toga.Label('', style=Pack(padding=(0, 0, 5, 0)))
        self.lbl_amount   = toga.Label('', style=Pack(padding=(0, 0, 5, 0)))
        self.lbl_creditor = toga.Label('', style=Pack(padding=(0, 0, 5, 0)))
        self.lbl_text     = toga.Label('', style=Pack(padding=(0, 0, 5, 0)))

        left_container.add(self.lbl_voucher)
        left_container.add(self.lbl_date)
        left_container.add(self.lbl_amount)
        left_container.add(self.lbl_creditor)
        left_container.add(self.lbl_text)

        # Center panel: candidates table (File, Dates, Amounts, Vendors)
        self.table = toga.Table(
            headings=['File', 'Dates', 'Amounts', 'Vendors'],
            accessors=['file', 'dates', 'amounts', 'vendors'],
            missing_value='',
            style=Pack(flex=1, padding=10),
            on_select=self.show_document_preview
        )

        # Right panel: Preview scroll (for PDF first page or image)
        self.preview_scroll = toga.ScrollContainer(style=Pack(flex=1, padding=10))
        self.preview_scroll.content = toga.Label(
            'Select a document to preview',
            style=Pack(margin=10)
        )

        # PDF nav buttons (Previous Page / Next Page)
        pdf_nav_box = toga.Box(style=Pack(direction=ROW, padding=5))
        self.btn_pdf_prev = toga.Button(
            "« Prev Page",
            on_press=self.prev_pdf_page,
            style=Pack(padding=5, width=100)
        )
        self.btn_pdf_next = toga.Button(
            "Next Page »",
            on_press=self.next_pdf_page,
            style=Pack(padding=5, width=100)
        )
        pdf_nav_box.add(self.btn_pdf_prev)
        pdf_nav_box.add(self.btn_pdf_next)

        right_container = toga.Box(style=Pack(direction=COLUMN, flex=1))
        right_container.add(self.table)
        right_container.add(self.preview_scroll)
        right_container.add(pdf_nav_box)

        # Bottom buttons: Previous Voucher, Next Voucher, Match, Save & Exit
        button_box = toga.Box(style=Pack(direction=ROW, padding=10))
        self.btn_prev  = toga.Button('← Previous Voucher', on_press=self.prev_record, style=Pack(padding=5, width=150))
        self.btn_next  = toga.Button('Next Voucher →',     on_press=self.next_record, style=Pack(padding=5, width=150))
        self.btn_match = toga.Button('Match This Voucher', on_press=self.match_record, style=Pack(padding=5, width=150))
        self.btn_save  = toga.Button('Save & Exit',         on_press=self.save_and_exit, style=Pack(padding=5, width=120))

        button_box.add(self.btn_prev)
        button_box.add(self.btn_next)
        button_box.add(self.btn_match)
        button_box.add(self.btn_save)

        # Put everything together in a horizontal split: left (voucher info) + right (table + preview)
        split_box = toga.Box(style=Pack(direction=ROW, padding=0, flex=1))
        split_box.add(left_container)
        split_box.add(right_container)

        main_box = toga.Box(style=Pack(direction=COLUMN, flex=1))
        main_box.add(split_box)
        main_box.add(button_box)

        self.main_window.content = main_box
        self.main_window.show()

        # Show the first unmatched voucher (if any)
        if self.unmatched_vouchers:
            self.show_record()
        else:
            self.main_window.info_dialog("All Matched", "There are no unmatched vouchers remaining.")
            self.main_window.close()

    # -----------------------------------------------------------------------------
    #  Show the current voucher + its candidate documents in the table
    # -----------------------------------------------------------------------------
    def show_record(self):
        """
        Display voucher info (VoucherNumber, Date, Amount, Creditor Name, Text),
        then compute the candidate set of documents:
          • Any doc whose amount matches exactly, OR
          • Any doc whose vendor lines match the creditor’s aliases, OR
          • Any doc whose date is within ±30 days of voucher date
        (This is just for the UI; automatic passes already updated matchinfo.json.)
        """
        if not self.unmatched_vouchers:
            return

        try:
            vn = self.unmatched_vouchers[self.current_index]
            voucher = self.voucher_map[vn]
            v_date = datetime.fromisoformat(voucher['Date_iso'])
            v_amount = voucher['Amount']
            cred_id = voucher['CreditorID']

            # Fill in the left‐side labels
            self.lbl_voucher.text  = f"Voucher #: {vn}"
            self.lbl_date.text     = f"Date: {voucher['Date_iso']}"
            self.lbl_amount.text   = f"Amount: {v_amount:.2f}"
            self.lbl_creditor.text = f"Creditor ID: {cred_id}"
            self.lbl_text.text     = f"Text: {voucher.get('Text', '')}"

            # Build a set of candidate docs (for visual inspection):
            # 1) All docs that contain the exact amount
            # 2) All docs whose vendor lines match any of this creditor’s aliases
            # 3) All docs whose closest date is within ±30 days (a bit broader for the UI)
            candidates_set: set[str] = set()

            # Index docs by amount
            for doc in self.doc_records:
                if vn in self.matchinfo['matches']:
                    # Already matched by auto-pass (should not be in unmatched)
                    continue

                if round(float(v_amount), 2) in [round(float(x), 2) for x in doc.get('amounts', [])]:
                    candidates_set.add(doc['file'])

            # Alias match (like pass B, but no strict date window here)
            if cred_id in self.creditors:
                cred = self.creditors[cred_id]
                alias_list = [ (a.get('prefix',''), a.get('postfix','')) for a in cred.get('aliases', []) ]
                for doc in self.doc_records:
                    if doc['file'] not in self.unmatched_docs:
                        continue
                    for line in doc.get('vendors', []):
                        for (pref, post) in alias_list:
                            if line.startswith(pref) and (not post or line.endswith(post)):
                                candidates_set.add(doc['file'])
                                break
                        if doc['file'] in candidates_set:
                            break

            # Date window match (±30 days)
            for doc in self.doc_records:
                if doc['file'] not in self.unmatched_docs:
                    continue
                doc_dates = []
                for ds in doc.get('dates', []):
                    try:
                        doc_dates.append(datetime.fromisoformat(ds))
                    except:
                        pass
                if not doc_dates:
                    continue
                closest = min(doc_dates, key=lambda dd: abs((dd - v_date).days))
                if abs((closest - v_date).days) <= 30:
                    candidates_set.add(doc['file'])

            # Convert to sorted list for display
            candidates = sorted(candidates_set)

            # Populate the table with each candidate’s metadata
            table_data = []
            for fn in candidates:
                # Find the doc record
                rec = next((d for d in self.doc_records if d['file'] == fn), None)
                if rec:
                    table_data.append({
                        'file':    rec['file'],
                        'dates':   ', '.join(rec.get('dates', [])[:5]),
                        'amounts': ', '.join(f"{float(a):.2f}" for a in rec.get('amounts', [])[:5]),
                        'vendors': ', '.join(rec.get('vendors', [])[:5])
                    })
            self.table.data = table_data

            # Reset preview area
            self.preview_scroll.content = toga.Label('Select a document to preview', style=Pack(margin=10))

            logger.info(f"Displayed {len(candidates)} candidate docs for voucher {vn}")

        except Exception as e:
            logger.error(f"Error in show_record(): {e}\n{traceback.format_exc()}")
            sys.exit(1)

    # -----------------------------------------------------------------------------
    #  When user selects a row in the table, show a preview (first page if PDF, or the image)
    # -----------------------------------------------------------------------------
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
                # Convert PDF first page to image
                self.current_pdf_images = convert_from_path(file_path, first_page=1, last_page=1)
                self.current_pdf_file = file_path
                self.current_pdf_page = 0
                self.show_pdf_page()
            else:
                # Directly display image
                img = toga.Image(file_path)
                iv = toga.ImageView(img, style=Pack(width=400, height=600))
                self.preview_scroll.content = iv

        except Exception as e:
            logger.error(f"Error previewing {file_path}: {e}")
            self.preview_scroll.content = toga.Label("Failed to preview file", style=Pack(margin=10))

    def show_pdf_page(self):
        """
        Display the current page of a multi‐page PDF (navigated by Next/Prev buttons).
        """
        if not self.current_pdf_images:
            return
        try:
            page_img = self.current_pdf_images[self.current_pdf_page]
            with NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                page_img.save(tmp.name, format='PNG')
                img = toga.Image(tmp.name)
                iv = toga.ImageView(img, style=Pack(width=400, height=600))
                self.preview_scroll.content = iv
        except Exception as e:
            logger.error(f"Error rendering PDF page: {e}")
            self.preview_scroll.content = toga.Label("Error rendering PDF page", style=Pack(margin=10))

    def next_pdf_page(self, widget):
        """
        If a PDF is loaded, advance one page (if possible) and show it.
        """
        if self.current_pdf_images and self.current_pdf_page < len(self.current_pdf_images) - 1:
            self.current_pdf_page += 1
            self.show_pdf_page()

    def prev_pdf_page(self, widget):
        """
        If a PDF is loaded, go back one page (if possible) and show it.
        """
        if self.current_pdf_images and self.current_pdf_page > 0:
            self.current_pdf_page -= 1
            self.show_pdf_page()

    # -----------------------------------------------------------------------------
    #  Navigation: Previous / Next Voucher buttons
    # -----------------------------------------------------------------------------
    def prev_record(self, widget):
        if self.current_index > 0:
            self.current_index -= 1
            self.show_record()

    def next_record(self, widget):
        if self.current_index < len(self.unmatched_vouchers) - 1:
            self.current_index += 1
            self.show_record()

    # -----------------------------------------------------------------------------
    #  Match button: record the selected document as matched to the current voucher
    # -----------------------------------------------------------------------------
    def match_record(self, widget):
        if not self.table.selection:
            self.main_window.info_dialog('Error', 'No document selected.')
            return

        vn = self.unmatched_vouchers[self.current_index]
        doc_file = self.table.selection.file

        # Record the match
        if vn not in self.matchinfo['matches']:
            self.matchinfo['matches'][vn] = [doc_file]
        else:
            # If it already had a list, append (though normally it shouldn’t)
            if doc_file not in self.matchinfo['matches'][vn]:
                self.matchinfo['matches'][vn].append(doc_file)

        # Remove from our in‐memory “unmatched” lists
        if vn in self.unmatched_vouchers:
            self.unmatched_vouchers.remove(vn)
        if doc_file in self.unmatched_docs:
            self.unmatched_docs.remove(doc_file)

        logger.info(f"Manually matched voucher {vn} → {doc_file}")
        save_matchinfo(self.matchinfo_file, self.matchinfo)

        # Adjust current_index so we don’t skip or go out of range
        if self.current_index >= len(self.unmatched_vouchers):
            self.current_index = max(0, len(self.unmatched_vouchers) - 1)

        # If there are still vouchers left, show the next; otherwise close
        if self.unmatched_vouchers:
            self.show_record()
        else:
            self.main_window.info_dialog("All Done", "All vouchers have now been matched.")
            self.main_window.close()

    # -----------------------------------------------------------------------------
    #  Save & Exit button: write JSON and close
    # -----------------------------------------------------------------------------
    def save_and_exit(self, widget):
        save_matchinfo(self.matchinfo_file, self.matchinfo)
        self.main_window.info_dialog("Saved", "Match information saved.")
        self.main_window.close()

    # -----------------------------------------------------------------------------
    #  on_exit hook: always save (no more unmatched‐vouchers checks)
    # -----------------------------------------------------------------------------
    def on_exit(self):
        save_matchinfo(self.matchinfo_file, self.matchinfo)
        return True  # allow exit


def main():
    logger.debug("Entering main()")
    try:
        args = parse_arguments()

        # Ensure any output directories exist (e.g. for bank_file, docdata_json, etc.)
        for file_key in ['bank_file', 'docdata_json', 'matchinfo_file', 'creditors_file']:
            out_dir = os.path.dirname(args[file_key])
            if out_dir and not os.path.exists(out_dir):
                logger.info(f"Creating directory: {out_dir}")
                os.makedirs(out_dir)

        if not check_display():
            logger.warning("Headless mode: will only test file loading.")
            if not test_file_loading(
                args['bank_file'],
                args['docdata_json'],
                args['creditors_file'],
                args['matchinfo_file']
            ):
                logger.error("File loading test failed; exiting.")
                sys.exit(1)
            logger.info("File loading test succeeded. Exiting (GUI is required to proceed).")
            sys.exit(0)

        return GrokMatcher(
            args['doc_folder'],
            args['bank_file'],
            args['docdata_json'],
            args['matchinfo_file'],
            args['creditors_file']
        )

    except Exception as e:
        logger.error(f"Error initializing application: {e}\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == '__main__':
    app = main()
    if app:
        app.main_loop()
