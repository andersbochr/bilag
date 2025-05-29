#!/usr/bin/env python3
import re
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from enum import Enum

class MpType(Enum):
    PAYMENT = 'payment'
    FEE     = 'fee'

@dataclass
class BankEntry:
    date: datetime
    amount: float
    text: str

@dataclass
class MpEntry:
    date: datetime
    amount: float
    type: MpType
    transfer_ref: str
    customer_ref: str
    message: str

@dataclass
class CashEntry:
    date: datetime
    amount: float
    invoice: str

@dataclass
class Alias:
    creditor_id: int
    prefix: str
    postfix: str
    debit_account: str
    credit_account: str
    override: str

@dataclass
class Creditor:
    id: int
    name: str
    single_voucher: bool
    aliases: list[Alias] = field(default_factory=list)

# --- KONSTANTER ---
SEPARATOR = ';'
BANK_ACCOUNT = '58000'
SALES_ACCOUNT = '1000'
REGISTER_ACCOUNT = '90000'
FEE_ACCOUNT = '7200'

BANK_DEB_AUTOMATINDBETALING_TEXT_PREFIX   = 'Automatindbetaling Aarhus'
MOBILEPAY_NAME                            = 'mobilepay'

BANK_DEB_DEFAULT_DEBIT_ACCOUNT            = BANK_ACCOUNT
BANK_DEB_DEFAULT_CREDIT_ACCOUNT           = SALES_ACCOUNT
MP_DEB_DEFAULT_DEBIT_ACCOUNT              = BANK_ACCOUNT
MP_DEB_DEFAULT_CREDIT_ACCOUNT             = SALES_ACCOUNT
MP_KRED_DEFAULT_DEBIT_ACCOUNT             = FEE_ACCOUNT
MP_KRED_DEFAULT_CREDIT_ACCOUNT            = BANK_ACCOUNT

# ------------------

def parse_date(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Ukendt dato-format: {s}")

def parse_amount(s: str) -> float:
    s = s.strip().replace(' ', '')
    # Hvis både punktum og komma → dansk format: punktum tusind, komma decimal
    if '.' in s and ',' in s:
        s = s.replace('.', '').replace(',', '.')
    # Kun komma → decimal
    elif ',' in s:
        s = s.replace(',', '.')
    # Kun punktum → decimal (lad være med at fjerne det)
    return float(s)

def pick_col(headers: list[str], da: str, en: str) -> str:
    return da if da in headers else en

def clean_account(acc: str) -> str:
    """Fjern trailing '.0' hvis det er fra Pandas-ekstrakt."""
    acc = acc.strip()
    return acc[:-2] if acc.endswith('.0') else acc

def load_bank(path: Path) -> list[BankEntry]:
    rows = []
    with path.open(newline='', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        hdr = rd.fieldnames or []
        dc = pick_col(hdr, 'Dato', 'Date')
        ac = pick_col(hdr, 'Beløb', 'Amount')
        tc = pick_col(hdr, 'Tekst', 'Text')
        for r in rd:
            rows.append(BankEntry(
                date   = parse_date(r[dc]),
                amount = parse_amount(r[ac]),
                text   = r[tc].strip()
            ))
    return rows

def load_mp(path: Path) -> list[MpEntry]:
    rows = []
    with path.open(newline='', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        hdr = rd.fieldnames or []
        dc  = pick_col(hdr, 'Overførsels Dato', 'Date')
        ac  = pick_col(hdr, 'Beløb', 'Amount')
        tc  = pick_col(hdr, 'Type', 'Type')
        trc = pick_col(hdr, 'Overførsels Reference', 'Transfer Reference')
        crc = pick_col(hdr, 'Transaktions ID', 'Customer Reference')
        mc  = pick_col(hdr, 'Besked', 'Message')
        for r in rd:
            raw = r[tc].strip().lower()
            tp  = MpType.FEE if raw == 'fee' else MpType.PAYMENT
            rows.append(MpEntry(
                date         = parse_date(r[dc]),
                amount       = parse_amount(r[ac]),
                type         = tp,
                transfer_ref = r.get(trc, '').strip(),
                customer_ref = r.get(crc, '').strip(),
                message      = r.get(mc, '').strip()
            ))
    return rows

def load_cash(path: Path) -> list[CashEntry]:
    rows = []
    with path.open(newline='', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        hdr = rd.fieldnames or []
        dc  = pick_col(hdr, 'Dato', 'Date')
        ac  = pick_col(hdr, 'Beløb', 'Amount')
        ic  = pick_col(hdr, 'Faktura', 'Invoice')
        for r in rd:
            rows.append(CashEntry(
                date    = parse_date(r[dc]),
                amount  = parse_amount(r[ac]),
                invoice = r.get(ic, '').strip()
            ))
    return rows

def load_creditors(json_path: Path) -> dict[int, Creditor]:
    data = json.loads(json_path.read_text(encoding='utf-8'))
    creditors: dict[int, Creditor] = {}
    for c in data.get('creditors', []):
        cr = Creditor(id=int(c['id']), name=c['name'], single_voucher=bool(c['single_voucher']))
        for a in c.get('aliases', []):
            cr.aliases.append(Alias(
                creditor_id   = cr.id,
                prefix        = a['prefix'],
                postfix       = a['postfix'],
                debit_account = clean_account(a['debit_account']),
                credit_account= clean_account(a['credit_account']),
                override      = a.get('override', '')
            ))
        creditors[cr.id] = cr
    return creditors

def match_and_split(entries: list[BankEntry],
                    creditors: dict[int, Creditor],
                    assign_voucher: bool=False):
    kred, deb = [], []
    unmatched = []
    voucher_counter = 1
    voucher_map = {}

    for e in entries:
        matched = False
        for cr in creditors.values():
            for al in cr.aliases:
                if e.text.startswith(al.prefix) and e.text.endswith(al.postfix):
                    # bilagslogik
                    if assign_voucher:
                        if cr.single_voucher:
                            if cr.id not in voucher_map:
                                voucher_map[cr.id] = voucher_counter
                                voucher_counter += 1
                            v = voucher_map[cr.id]
                        else:
                            v = voucher_counter
                            voucher_counter += 1

                    # kun override ved negativt beløb
                    text = al.override if (e.amount < 0 and al.override) else e.text

                    row = {
                        'Date':          e.date.date().isoformat(),
                        'Amount':        abs(e.amount),
                        'CreditorID':    cr.id,
                        'DebitAccount':   al.debit_account,
                        'CreditAccount':  al.credit_account,
                        'Text':          text
                    }
                    if assign_voucher:
                        row['VoucherNumber'] = v

                    (kred if e.amount < 0 else deb).append(row)
                    matched = True
                    break
            if matched:
                break
        if not matched:
            unmatched.append(e)

    return kred, deb, unmatched



def sum_mobilepay_per_day(mp: list[MpEntry]):
    fees, pays = defaultdict(float), defaultdict(float)
    for e in mp:
        d = e.date.date().isoformat()
        if e.type == MpType.FEE:
            fees[d] += e.amount
        else:
            pays[d] += e.amount
    return fees, pays

def format_number(n: float) -> str:
    # Always two decimals, comma as decimal separator
    return f"{n:.2f}".replace('.', ',')

def write_csv(path: Path, rows: list[dict], default_hdr: list[str]):
    hdr = list(rows[0].keys()) if rows else default_hdr
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=hdr, delimiter=SEPARATOR, lineterminator='\n')
        w.writeheader()
        for r in rows:
            out = {}
            for k, v in r.items():
                # format numeric fields
                if k in ('Amount', 'TotalFee', 'TotalPayment'):
                    out[k] = format_number(v)
                else:
                    out[k] = v
            w.writerow(out)


def main(input_dir: Path, json_path: Path, output_dir: Path):
  

    # 1) Indlæs data
    bank      = load_bank(input_dir/'bank.csv')
    mp        = load_mp(input_dir/'mp.csv')
    cash      = load_cash(input_dir/'kontant.csv')
    creditors = load_creditors(json_path)

    # 2) Find Mobilepay-kreditor
    try:
        mobilepay_cr = next(cr for cr in creditors.values()
                            if cr.name.lower() == MOBILEPAY_NAME)
    except StopIteration:
        raise RuntimeError("Kreditor 'Mobilepay' ikke fundet i creditors.json")

    # 3) Initialiser voucher-tracking
    voucher_counter = 1
    voucher_map     = {}

    # 4) Processér bank-posteringer
    bank_kred, bank_deb, bank_unmatched = [], [], []
    for e in bank:
        if e.text.startswith('0200448585'):
            continue  # spring MobilePay-indbetalinger

        if e.amount < 0:
            # match negative beløb mod kreditorer
            matched = False
            for cr in creditors.values():
                for al in cr.aliases:
                    if e.text.startswith(al.prefix) and e.text.endswith(al.postfix):
                        # voucher-logik
                        if cr.single_voucher:
                            if cr.id not in voucher_map:
                                voucher_map[cr.id] = voucher_counter
                                voucher_counter += 1
                            v = voucher_map[cr.id]
                        else:
                            v = voucher_counter
                            voucher_counter += 1

                        text = al.override or e.text
                        bank_kred.append({
                            'VoucherNumber': v,
                            'Date':          e.date.strftime("%d-%m-%Y"),
                            'Amount':        abs(e.amount),
                            'CreditorID':    cr.id,
                            'DebitAccount':   clean_account(al.debit_account),
                            'CreditAccount':  clean_account(al.credit_account),
                            'Text':          text
                        })
                        matched = True
                        break
                if matched: break

            if not matched:
                bank_unmatched.append(e)
                v = voucher_counter; voucher_counter += 1
                bank_kred.append({
                    'VoucherNumber': v,
                    'Date':          e.date.strftime("%d-%m-%Y"),
                    'Amount':        abs(e.amount),
                    'CreditorID':    '',
                    'DebitAccount':   '',
                    'CreditAccount':  '',
                    'Text':          e.text
                })

        else:
            # positive beløb → bank_deb
            cred_acc = (REGISTER_ACCOUNT
                         if e.text.startswith(BANK_DEB_AUTOMATINDBETALING_TEXT_PREFIX)
                         else SALES_ACCOUNT)
            v = voucher_counter; voucher_counter += 1
            bank_deb.append({
                'VoucherNumber': v,
                'Date':          e.date.strftime("%d-%m-%Y"),
                'Amount':        e.amount,
                'CreditorID':    '',
                'DebitAccount':   BANK_ACCOUNT,
                'CreditAccount':  cred_acc,
                'Text':          e.text
            })

    if bank_unmatched:
        print(f"Warning: {len(bank_unmatched)} negative bank-posteringer manglede alias:")
        for e in bank_unmatched[:5]:
            print(f"  {e.text}")

    # 5) Processér MobilePay-posteringer
    mp_kred, mp_deb = [], []
    for e in mp:
        amt = -e.amount if e.type == MpType.FEE else e.amount

        if amt < 0:
            # gebyrer → mp_kred
            matched = False
            for cr in creditors.values():
                for al in cr.aliases:
                    if e.message.startswith(al.prefix) and e.message.endswith(al.postfix):
                        cr_id, debit_acc, credit_acc = cr.id, MP_KRED_DEFAULT_DEBIT_ACCOUNT, MP_KRED_DEFAULT_CREDIT_ACCOUNT
                        text = al.override or e.message
                        matched = True
                        break
                if matched: break

            if not matched:
                cr_id, debit_acc, credit_acc = mobilepay_cr.id, MP_KRED_DEFAULT_DEBIT_ACCOUNT, MP_KRED_DEFAULT_CREDIT_ACCOUNT
                text = e.message

            # voucher-logik
            if creditors[cr_id].single_voucher:
                if cr_id not in voucher_map:
                    voucher_map[cr_id] = voucher_counter
                    voucher_counter += 1
                v = voucher_map[cr_id]
            else:
                v = voucher_counter; voucher_counter += 1

            mp_kred.append({
                'VoucherNumber': v,
                'Date':          e.date.strftime("%d-%m-%Y"),
                'Amount':        abs(amt),
                'CreditorID':    cr_id,
                'DebitAccount':   debit_acc,
                'CreditAccount':  credit_acc,
                'Text':          'Mobilepay Gebyr'
            })

        else:
            # betalinger → mp_deb med mønster-udtræk
            m = re.search(r'\b(\d{4}-\d{3})\b', e.message)
            txt = m.group(1) if m else e.customer_ref
            v = voucher_counter; voucher_counter += 1
            mp_deb.append({
                'VoucherNumber': v,
                'Date':          e.date.strftime("%d-%m-%Y"),
                'Amount':        amt,
                'CreditorID':    '',
                'DebitAccount':   MP_DEB_DEFAULT_DEBIT_ACCOUNT,
                'CreditAccount':  MP_DEB_DEFAULT_CREDIT_ACCOUNT,
                'Text':          txt
            })

    # 6) Processér kontant-posteringer
    cash_kred, cash_deb = [], []
    for e in cash:
        target = cash_kred if e.amount < 0 else cash_deb
        v = voucher_counter; voucher_counter += 1
        target.append({
            'VoucherNumber': v,
            'Date':          e.date.strftime("%d-%m-%Y"),
            'Amount':        abs(e.amount),
            'CreditorID':    '',
            'DebitAccount':   REGISTER_ACCOUNT,
            'CreditAccount':  SALES_ACCOUNT,
            'Text':          e.invoice
        })

    # 7) Skriv output-filer
    output_dir.mkdir(exist_ok=True)
    hdr = ['VoucherNumber','Date','Amount','CreditorID','DebitAccount','CreditAccount','Text']

    write_csv(output_dir/'bank_kred.csv', bank_kred, hdr)
    write_csv(output_dir/'bank_deb.csv',  bank_deb,  hdr)
    write_csv(output_dir/'mp_kred.csv',    mp_kred,   hdr)
    write_csv(output_dir/'mp_deb.csv',     mp_deb,    hdr)
    write_csv(output_dir/'kont_kred.csv',  cash_kred, hdr)
    write_csv(output_dir/'kont_deb.csv',   cash_deb,  hdr)

    # 8) Daglige MobilePay-opsummeringer
    fees, pays = sum_mobilepay_per_day(mp)
    fee_rows   = [{'Date': d, 'TotalFee': amt} for d, amt in fees.items()]
    pay_rows   = [{'Date': d, 'TotalPayment': amt} for d, amt in pays.items()]
    write_csv(output_dir/'mp_fee_daily.csv', fee_rows, ['Date','TotalFee'])
    write_csv(output_dir/'mp_pay_daily.csv', pay_rows, ['Date','TotalPayment'])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Processér bank/mp/kontant med vouchers')
    parser.add_argument('input_dir',      type=Path, help='Mappe med bank.csv, mp.csv, kontant.csv')
    parser.add_argument('creditors_json', type=Path, help='Sti til creditors.json')
    parser.add_argument('output_dir',     type=Path, help='Mappe til output CSV-filer')
    args = parser.parse_args()
    main(args.input_dir, args.creditors_json, args.output_dir)
