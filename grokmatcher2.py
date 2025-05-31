import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
import csv
import json
import os
import sys
from datetime import datetime
import logging
import traceback
from datetime import datetime
from pdf2image import convert_from_path
from tempfile import NamedTemporaryFile

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Set __package__ to avoid Toga initialization error
__package__ = "grokmatcher"

def check_display():
    logger.debug("Checking display server")
    display = os.environ.get('DISPLAY')
    if not display:
        logger.warning("No display server detected (DISPLAY not set)")
        return False
    logger.info(f"Display server detected: {display}")
    return True

def validate_file(file_path):
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
    logger.debug(f"Received arguments: {sys.argv[1:]}")
    if len(sys.argv) != 6:
        logger.error(f"Usage: {sys.argv[0]} <doc_folder> <bank_file> <docdata_json> <matchinfo_file> <creditors_file>")
        sys.exit(1)
    args = {
        'doc_folder': sys.argv[1],
        'bank_file': sys.argv[2],
        'docdata_json': sys.argv[3],
        'matchinfo_file': sys.argv[4],
        'creditors_file': sys.argv[5]
    }
    for key, path in args.items():
        if key != 'doc_folder' and not validate_file(path):
            sys.exit(1)
    if not os.path.isdir(args['doc_folder']):
        logger.error(f"Document folder not found: {args['doc_folder']}")
        sys.exit(1)
    return args

def load_doc_records(docdata_json):
    logger.debug(f"Loading document data from {docdata_json}")
    try:
        with open(docdata_json, 'r', encoding='utf-8') as f:
            docs = json.load(f)
        for doc in docs:
            doc['amounts'] = [float(a) for a in doc.get('amounts', []) if isinstance(a, (int, float, str)) and str(a).replace('.', '').replace('-', '').isdigit()]
            doc['vendors'] = doc.get('vendors', [])
            doc['dates'] = doc.get('dates', [])
        if not docs:
            logger.error(f"{docdata_json} is empty")
            sys.exit(1)
        logger.info(f"Loaded {len(docs)} document records")
        return docs
    except FileNotFoundError:
        logger.error(f"{docdata_json} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"{docdata_json} contains invalid JSON: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading {docdata_json}: {e}\n{traceback.format_exc()}")
        sys.exit(1)

def load_bank_records(bank_file):
    logger.debug(f"Loading bank records from {bank_file}")
    try:
        bank_records = []
        with open(bank_file, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile, delimiter=';')
            for row in reader:
                date_str = row.get('Date', '')
                try:
                    if '-' in date_str:
                        # Format: DD-MM-YYYY
                        day, month, year = date_str.split('-')
                    elif '.' in date_str:
                        # Format: DD.MM.YY or DD.MM.YYYY
                        day, month, year = date_str.split('.')
                    else:
                        raise ValueError("Unknown date format")

                    if len(year) == 2:
                        year = f"20{year}"  # Convert YY to YYYY
                    iso_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                    datetime.fromisoformat(iso_date)
                except (ValueError, IndexError):
                    logger.warning(f"Skipping row with invalid date format: {date_str}")
                    continue
                try:
                    amount = float(row['Amount'].replace(' ', '').replace(',', '.'))
                except (ValueError, KeyError):
                    logger.warning(f"Skipping row with invalid amount: {row.get('Amount', '')}")
                    continue
                bank_records.append({
                    'VoucherNumber': row['VoucherNumber'],
                    'Date_iso': iso_date,
                    'Amount': amount,
                    'CreditorID': int(row['CreditorID']),
                    'DebitAccount': row.get('DebitAccount', ''),
                    'CreditAccount': row.get('CreditAccount', ''),
                    'Text': row.get('Text', '')
                })
        if not bank_records:
            logger.error(f"No valid records found in {bank_file}")
            sys.exit(1)
        logger.info(f"Loaded {len(bank_records)} bank records")
        return bank_records
    except FileNotFoundError:
        logger.error(f"{bank_file} not found")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading {bank_file}: {e}\n{traceback.format_exc()}")
        sys.exit(1)

def load_creditors(creditors_file):
    logger.debug(f"Loading creditor file {creditors_file}")
    try:
        with open(creditors_file, 'r', encoding='utf-8') as f:
            creditors = json.load(f)
        return {c['id']: c for c in creditors}
    except FileNotFoundError:
        logger.error(f"Error: {creditors_file} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error: {creditors_file} contains invalid JSON: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading {creditors_file}: {e}\n{traceback.format_exc()}")
        sys.exit(1)

def load_matchinfo(matchinfo_file):
    logger.debug(f"Loading matchinfo from {matchinfo_file}")
    try:
        if not os.path.exists(matchinfo_file):
            logger.warning(f"Matchinfo file {matchinfo_file} not found, initializing as empty")
            return {'matches': {}, 'unmatchedVouchers': [], 'unmatchedDocs': []}
        with open(matchinfo_file, 'r', encoding='utf-8') as f:
            matchinfo = json.load(f)
        logger.info(f"Matchinfo loaded: {len(matchinfo.get('matches', {}))} matches, {len(matchinfo.get('unmatchedVouchers', []))} unmatched vouchers")
        return matchinfo
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading {matchinfo_file}: {e}\n{traceback.format_exc()}")
        return {'matches': {}, 'unmatchedVouchers': [], 'unmatchedDocs': []}

def save_matchinfo(matchinfo_file, matchinfo):
    logger.debug(f"Saving matchinfo to {matchinfo_file}")
    try:
        with open(matchinfo_file, 'w', encoding='utf-8') as f:
            json.dump(matchinfo, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error(f"Error saving matchinfo to {matchinfo_file}: {e}\n{traceback.format_exc()}")

def pass_a_exact_amount(voucher_records, doc_records, voucher_numbers, unmatched_docs):
    matches = {}

    # Build doc index by amount (across all documents, matched or not)
    doc_by_amount = {}
    for doc in doc_records:
        for amt in doc.get('amounts', []):
            doc_by_amount.setdefault(round(float(amt), 2), []).append(doc['file'])

    # Match vouchers to docs with the same amount
    for voucher in voucher_records:
        vn = voucher['VoucherNumber']
        amount = round(float(voucher['Amount']), 2)
        candidates = doc_by_amount.get(amount, [])
        if candidates:
            matches[str(vn)] = candidates  # multiple vouchers can share a doc
    return matches



def pass_b_alias_date(bank_records, doc_records, unmatched_vouchers, unmatched_docs, creditors, existing_matches):
    from datetime import timedelta
    new_matches = {}
    for v in bank_records:
        vn = v['VoucherNumber']
        if vn not in unmatched_vouchers:
            continue
        cred = creditors.get(v['CreditorID'])
        if not cred:
            continue
        alias_list = [(a['prefix'], a.get('postfix', '')) for a in cred['aliases']]
        try:
            v_date = datetime.fromisoformat(v['Date_iso'])
        except ValueError:
            logger.warning(f"Skipping voucher {vn} with invalid date {v['Date_iso']}")
            continue
        candidates = [d for d in doc_records if d['file'] in unmatched_docs]
        matches = []
        for doc in candidates:
            textlines = doc['vendors']
            if any(line.startswith(pref) and (not post or line.endswith(post)) for (pref, post) in alias_list for line in textlines):
                doc_dates = [datetime.fromisoformat(date) for date in doc.get('dates', []) if date]
                if doc_dates:
                    doc_date = min(doc_dates, key=lambda d: abs((d - v_date).days))
                    if abs((doc_date - v_date).days) <= 15:
                        matches.append(doc['file'])
                else:
                    matches.append(doc['file'])
        if len(matches) == 1:
            new_matches[vn] = [matches[0]]
    return new_matches

def pass_c_subscription(bank_records, doc_records, unmatched_vouchers, unmatched_docs, creditors):
    from datetime import timedelta
    new_matches = {}
    frequency_deltas = {
        'monthly': timedelta(days=30),
        'quarterly': timedelta(days=90),
        'semi-annual': timedelta(days=180),
        'bimonthly': timedelta(days=60)
    }

    for v in bank_records:
        vn = v['VoucherNumber']
        if vn not in unmatched_vouchers:
            continue
        cred = creditors.get(v['CreditorID'])
        if not cred:
            continue
        subscription_aliases = [a for a in cred['aliases'] if a.get('frequency')]
        if not subscription_aliases:
            continue
        try:
            v_date = datetime.fromisoformat(v['Date_iso'])
        except ValueError:
            logger.warning(f"Skipping voucher {vn} with invalid date {v['Date_iso']}")
            continue
        v_amount = v['Amount']
        alias_list = [(a['prefix'], a.get('postfix', '')) for a in subscription_aliases]
        candidates = [d for d in doc_records if d['file'] in unmatched_docs]
        matches = []

        for doc in candidates:
            if v_amount not in doc['amounts']:
                continue
            if not any(line.startswith(pref) and (not post or line.endswith(post)) for (pref, post) in alias_list for line in doc['vendors']):
                continue
            doc_dates = [datetime.fromisoformat(date) for date in doc.get('dates', []) if date]
            for alias in subscription_aliases:
                frequency = alias.get('frequency')
                start_date_str = alias.get('start_date')
                if not frequency or not start_date_str:
                    continue
                try:
                    start_date = datetime.fromisoformat(start_date_str)
                except ValueError:
                    logger.warning(f"Invalid start_date {start_date_str} for creditor {cred['name']}")
                    continue
                delta = frequency_deltas.get(frequency, timedelta(days=30))
                expected_dates = []
                current_date = start_date
                while current_date <= v_date + delta:
                    if current_date >= start_date:
                        expected_dates.append(current_date)
                    current_date += delta
                if doc_dates:
                    doc_date = min(doc_dates, key=lambda d: abs((d - v_date).days))
                    if any(abs((doc_date - exp_date).days) <= 7 for exp_date in expected_dates):
                        matches.append(doc['file'])
                elif frequency == 'bimonthly' and abs((v_date - start_date).days) % 60 <= 7:
                    matches.append(doc['file'])

        if len(matches) == 1:
            new_matches[vn] = [matches[0]]

    return new_matches

def test_file_loading(bank_file, docdata_json, creditors_file, matchinfo_file):
    logger.info("Testing file loading in non-GUI mode")
    try:
        bank_records = load_bank_records(bank_file)
        logger.info(f"Bank records loaded successfully: {len(bank_records)} records")
        doc_records = load_doc_records(docdata_json)
        logger.info(f"Document records loaded successfully: {len(doc_records)} records")
        creditors = load_creditors(creditors_file)
        logger.info(f"Creditors loaded successfully: {len(creditors)} creditors")
        matchinfo = load_matchinfo(matchinfo_file)
        logger.info(f"Matchinfo loaded successfully: {len(matchinfo.get('matches', {}))} matches")
        return True
    except Exception as e:
        logger.error(f"File loading test failed: {e}\n{traceback.format_exc()}")
        return False

class GrokMatcher(toga.App):
    def __init__(self, doc_folder, bank_file, docdata_json, matchinfo_file, creditors_file):
        logger.debug("Initializing GrokMatcher")
        try:
            super().__init__('GrokMatcher', 'org.example.grokmatcher')
            self.doc_folder = doc_folder
            self.bank_file = bank_file
            self.docdata_json = docdata_json
            self.matchinfo_file = matchinfo_file
            self.creditors_file = creditors_file
            self.matchinfo = load_matchinfo(self.matchinfo_file)
            # Ensure unmatched_vouchers and unmatched_docs are consistent with matches
            matched_vouchers = set(self.matchinfo.get('matches', {}).keys())
            matched_docs = {doc for docs in self.matchinfo.get('matches', {}).values() for doc in docs}

            # Filter unmatched lists to remove any entries that are actually matched
            self.matchinfo['unmatchedVouchers'] = [
                int(vn) for vn in self.matchinfo.get('unmatchedVouchers', [])
                if str(vn) not in matched_vouchers
            ]
            self.matchinfo['unmatchedDocs'] = [
                doc for doc in self.matchinfo.get('unmatchedDocs', [])
                if doc not in matched_docs
            ]

            self.on_exit = self.on_exit
            logger.info("Initialized GrokMatcher application")
        except Exception as e:
            logger.error(f"Error initializing GrokMatcher: {e}\n{traceback.format_exc()}")
            sys.exit(1)

    def startup(self):
        logger.debug("Entering startup method")
        try:

            self.current_pdf_file = None
            self.current_pdf_images = []
            self.current_pdf_page = 0

            logger.debug("Creating main window")
            self.main_window = toga.MainWindow(title=self.formal_name)
            logger.debug("Main window created")
            
            logger.debug("Loading bank records")
            self.bank_records = load_bank_records(self.bank_file)
            logger.debug("Bank records loaded")
            
            logger.debug("Loading document records")
            self.doc_records = load_doc_records(self.docdata_json)
            logger.debug("Document records loaded")
            
            logger.debug("Loading creditors")
            self.creditors = load_creditors(self.creditors_file)
            logger.debug("Creditors loaded")
            
            logger.debug("Building voucher map")
            self.voucher_map = {r['VoucherNumber']: r for r in self.bank_records}
            logger.debug("Voucher map built")
            
            logger.debug("Initializing unmatched vouchers and documents")
            all_vouchers = set(self.voucher_map.keys())
            matched_vouchers = set(self.matchinfo.get('matches', {}).keys())
            self.unmatched_vouchers = (
                list(map(str, self.matchinfo.get('unmatchedVouchers', [])))
                if self.matchinfo.get('unmatchedVouchers')
                else list(all_vouchers - matched_vouchers)
            )
            all_docs = set(d['file'] for d in self.doc_records)
            matched_docs = set()
            for docs in self.matchinfo.get('matches', {}).values():
                matched_docs.update(docs)
            self.unmatched_docs = (
                self.matchinfo.get('unmatchedDocs', [])
                if self.matchinfo.get('unmatchedDocs')
                else list(all_docs - matched_docs)
            )
            self.current_index = 0
            logger.debug("Unmatched vouchers and documents initialized")
            
            logger.debug("Setting up GUI components")
            left_container = toga.Box(style=Pack(direction=COLUMN, padding=10))
            self.lbl_voucher = toga.Label('Voucher #:', style=Pack(padding=5))
            self.lbl_date = toga.Label('Date:', style=Pack(padding=5))
            self.lbl_amount = toga.Label('Amount:', style=Pack(padding=5))
            self.lbl_creditor = toga.Label('Creditor:', style=Pack(padding=5))
            self.lbl_text = toga.Label('Text:', style=Pack(padding=5))
            left_container.add(self.lbl_voucher, self.lbl_date, self.lbl_amount, self.lbl_creditor, self.lbl_text)
            
            self.table = toga.Table(
                headings=['File', 'Dates', 'Amounts', 'Vendors'],
                style=Pack(flex=1, padding=10),
                on_select=self.show_document_preview
            )
            self.preview_scroll = toga.ScrollContainer(style=Pack(flex=1, padding=10))
            self.preview_scroll.content = toga.Label('Select a document to preview', style=Pack(margin=10))

            pdf_nav_box = toga.Box(style=Pack(direction=ROW, padding=5))
            self.btn_pdf_prev = toga.Button("Previous Page", on_press=self.prev_pdf_page, style=Pack(padding=5))
            self.btn_pdf_next = toga.Button("Next Page", on_press=self.next_pdf_page, style=Pack(padding=5))
            pdf_nav_box.add(self.btn_pdf_prev, self.btn_pdf_next)




            right_container = toga.Box(style=Pack(direction=COLUMN, flex=1))
            right_container.add(self.table, self.preview_scroll)
            right_container.add(pdf_nav_box)
            
            button_box = toga.Box(style=Pack(direction=ROW, padding=10))
            self.btn_prev = toga.Button('Previous', on_press=self.prev_record, style=Pack(padding=5))
            self.btn_next = toga.Button('Next', on_press=self.next_record, style=Pack(padding=5))
            self.btn_match = toga.Button('Match', on_press=self.match_record, style=Pack(padding=5))
            self.btn_save = toga.Button('Save', on_press=self.save_state, style=Pack(padding=5))
            button_box.add(self.btn_prev, self.btn_next, self.btn_match, self.btn_save)
            
            main_box = toga.Box(style=Pack(direction=COLUMN))
            main_box.add(toga.Box(
                style=Pack(direction=ROW, flex=1),
                children=[left_container, right_container]
            ))
            main_box.add(button_box)
            
            logger.debug("Assigning main window content")
            self.main_window.content = main_box
            logger.debug("Main window content assigned")
            
            logger.debug("Showing main window")
            self.main_window.show()
            logger.debug("Main window shown")
            
            logger.info("GUI started successfully")
            self.show_record()
        except Exception as e:
            logger.error(f"Error in startup: {e}\n{traceback.format_exc()}")
            test_file_loading(self.bank_file, self.docdata_json, self.creditors_file, self.matchinfo_file)
            sys.exit(1)

    def show_record(self):
        logger.debug("Entering show_record")
        try:
            if not self.unmatched_vouchers:
                logger.info("No unmatched vouchers remaining")
                self.main_window.info_dialog('Done', 'No unmatched vouchers remaining.')
                self.preview_scroll.content = toga.Label('No voucher selected', style=Pack(margin=10))
                return

            vn = self.unmatched_vouchers[self.current_index]
            rec = self.voucher_map.get(vn)
            if not rec:
                logger.warning(f"Voucher {vn} not found in bank records")
                self.unmatched_vouchers.remove(vn)
                if vn in self.matchinfo['unmatchedVouchers']:
                    self.matchinfo['unmatchedVouchers'].remove(int(vn))
                save_matchinfo(self.matchinfo_file, self.matchinfo)
                self.show_record()
                return
            self.lbl_voucher.text = f"Voucher #: {vn}"
            self.lbl_date.text = f"Date: {rec['Date_iso']}"
            self.lbl_amount.text = f"Amount: {rec['Amount']:.2f}"
            cid = rec['CreditorID']
            cred = self.creditors.get(cid, {})
            cname = cred.get('name', 'Unknown')
            self.lbl_creditor.text = f"Creditor: {cname} (ID: {cid})"
            self.lbl_text.text = f"Text: {rec['Text']}"
            logger.info(f"Displaying voucher {vn} (Creditor ID: {cid}, Amount: {rec['Amount']})")

            candidates_set = set()
            vn = self.unmatched_vouchers[self.current_index]
            rec = self.voucher_map.get(vn)
            rec_with_vn = dict(rec, VoucherNumber=vn)


            matches_a = pass_a_exact_amount([rec_with_vn], self.doc_records, [vn], self.unmatched_docs)

            candidates_set.update(matches_a.get(vn, []))

            matches_b = pass_b_alias_date([rec], self.doc_records, [vn], self.unmatched_docs, self.creditors, self.matchinfo['matches'])
            candidates_set.update(matches_b.get(vn, []))

            matches_c = pass_c_subscription([rec], self.doc_records, [vn], self.unmatched_docs, self.creditors)
            candidates_set.update(matches_c.get(vn, []))    

            candidates = list(candidates_set)

            self.table.data = [
                {
                    'file': d['file'],
                    'dates': ', '.join(d['dates']),
                    'amounts': ', '.join(f"{a:.2f}" for a in d['amounts']),
                    'vendors': ', '.join(d['vendors'][:5])
                }
                for d in self.doc_records if d['file'] in candidates
            ]
            self.preview_scroll.content = toga.Label('Select a document to preview', style=Pack(margin=10))
            logger.info(f"Displayed {len(candidates)} candidate documents for voucher {vn}")
        except Exception as e:
            logger.error(f"Error in show_record: {e}\n{traceback.format_exc()}")
            sys.exit(1)

    def show_pdf_page(self):
        if not self.current_pdf_images:
            return
        page_image = self.current_pdf_images[self.current_pdf_page]
        with NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            page_image.save(tmp_file.name, format='PNG')
            image = toga.Image(tmp_file.name)
        image_view = toga.ImageView(image, style=Pack(width=400, height=600))
        self.preview_scroll.content = image_view

    def next_pdf_page(self, widget):
        if self.current_pdf_page < len(self.current_pdf_images) - 1:
            self.current_pdf_page += 1
            self.show_pdf_page()

    def prev_pdf_page(self, widget):
        if self.current_pdf_page > 0:
            self.current_pdf_page -= 1
            self.show_pdf_page()

    def show_document_preview(self, widget):
        row = self.table.selection
        if row is None:
            self.preview_scroll.content = toga.Label("No document selected", style=Pack(margin=10))
            return

        file_path = os.path.join(self.doc_folder, row.file)
        logger.info(f"Previewing document: {file_path}")

        if file_path.lower().endswith('.pdf'):
            try:
                self.current_pdf_images = convert_from_path(file_path)
                self.current_pdf_file = file_path
                self.current_pdf_page = 0
                self.show_pdf_page()
            except Exception as e:
                logger.error(f"Error rendering PDF {file_path}: {e}")
                self.preview_scroll.content = toga.Label("Failed to preview PDF", style=Pack(margin=10))
        else:
            try:
                image = toga.Image(file_path)
                image_view = toga.ImageView(image, style=Pack(width=400, height=600))
                self.preview_scroll.content = image_view
            except Exception as e:
                logger.error(f"Could not load image {file_path}: {e}")
                self.preview_scroll.content = toga.Label("Failed to load image", style=Pack(margin=10))

    def prev_record(self, widget):
        logger.debug("Previous record requested")
        if self.current_index > 0:
            self.current_index -= 1
            self.show_record()

    def next_record(self, widget):
        logger.debug("Next record requested")
        if self.current_index < len(self.unmatched_vouchers) - 1:
            self.current_index += 1
            self.show_record()

    def match_record(self, widget):
        logger.debug("Match record requested")
        try:
            if not self.table.selection:
                self.main_window.info_dialog('Error', 'No document selected.')
                logger.warning("Match attempted with no document selected")
                return
            vn = self.unmatched_vouchers[self.current_index]
            doc_file = self.table.selection.file
            self.matchinfo['matches'][vn] = [doc_file]
            if vn in self.matchinfo['unmatchedVouchers']:
                self.matchinfo['unmatchedVouchers'].remove(int(vn))
            if doc_file in self.matchinfo['unmatchedDocs']:
                self.matchinfo['unmatchedDocs'].remove(doc_file)
            if vn in self.unmatched_vouchers:
                self.unmatched_vouchers.remove(vn)
            if doc_file in self.unmatched_docs:
                self.unmatched_docs.remove(doc_file)
            save_matchinfo(self.matchinfo_file, self.matchinfo)
            logger.info(f"Matched voucher {vn} to document {doc_file}")
            if self.current_index >= len(self.unmatched_vouchers):
                self.current_index = max(0, len(self.unmatched_vouchers) - 1)
            self.show_record()
        except Exception as e:
            logger.error(f"Error in match_record: {e}\n{traceback.format_exc()}")
            sys.exit(1)
    def save_state(self, widget=None):
        logger.info("Saving matchinfo file")
        save_matchinfo(self.matchinfo_file, self.matchinfo)
        self.main_window.info_dialog("Saved", "Match information saved.")

    def on_exit(self):
        if self.matchinfo.get('unmatchedVouchers') or self.matchinfo.get('unmatchedDocs'):
            if self.main_window.confirm_dialog("Save", "Do you want to save before exiting?"):
                self.save_state()
        return True  # allow exit        

def main():
    logger.debug("Entering main function")
    try:
        args = parse_arguments()
        logger.info("Starting GrokMatcher with arguments:")
        for key, value in sorted(args.items()):
            logger.info(f"  {key}: {value}")
        
        for file_key in ['bank_file', 'docdata_json']:
            outputs_dir = os.path.dirname(args[file_key])
            if outputs_dir and not os.path.exists(outputs_dir):
                logger.info(f"Creating outputs directory: {outputs_dir}")
                os.makedirs(outputs_dir)
        
        if not check_display():
            logger.warning("No display server, running in non-GUI mode")
            if not test_file_loading(
                args['bank_file'],
                args['docdata_json'],
                args['creditors_file'],
                args['matchinfo_file']
            ):
                logger.error("File loading failed, exiting")
                sys.exit(1)
            logger.info("File loading test passed, but GUI is required to proceed")
            sys.exit(0)
        
        return GrokMatcher(
            args['doc_folder'],
            args['bank_file'],
            args['docdata_json'],
            args['matchinfo_file'],
            args['creditors_file'],
        )
    except Exception as e:
        logger.error(f"Error initializing application: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == '__main__':
    app = main()
    if app:  # Only run if app was created (i.e., not in headless test mode)
        app.main_loop()    