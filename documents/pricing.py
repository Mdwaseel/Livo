"""
Money for priced documents (invoice, quotation).

The rule this module exists to enforce: **the AI never does arithmetic**. A
language model is good at reading "two landing pages at 25k and a year of
hosting" out of a brief and bad at adding it up — so it proposes rows, and
every figure a client ever sees is computed here in `Decimal`, from those rows.

`render_body` then rebuilds the document's HTML from the same rows, which is
why the table in the PDF and the totals in the editor cannot drift apart:
they are the same computation, run once.

Amounts are quantised to 2dp with ROUND_HALF_UP at every step (line, then tax,
then total) rather than once at the end, because that is what a printed invoice
shows — a total that doesn't equal the visible column added up is a support
ticket, even when it is "more accurate".
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.template.loader import render_to_string

TWO_DP = Decimal("0.01")
DEFAULT_TAX_RATE = Decimal("18")        # India's common services GST slab

# DecimalField(max_digits=12, decimal_places=2) — anything past this is a typo
# or an attack, and clamping beats a 500 from the database.
MAX_AMOUNT = Decimal("9999999999.99")
MAX_QUANTITY = Decimal("999999.99")

CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED "}
# Currencies we can spell out, and what their major/minor units are called.
CURRENCY_WORDS = {"INR": ("Rupees", "Paise")}


def money(value):
    """Any number as a 2dp Decimal, rounded the way invoices round."""
    try:
        return Decimal(value).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def to_decimal(raw, fallback=Decimal("0")):
    """A posted string as a Decimal. Tolerates the commas, spaces and currency
    symbols people paste in from a spreadsheet."""
    if isinstance(raw, Decimal):
        return raw
    cleaned = re.sub(r"[^\d.\-]", "", str(raw or ""))
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return fallback


def clamp(value, low, high):
    return max(low, min(high, value))


def currency_symbol(code):
    return CURRENCY_SYMBOLS.get((code or "INR").upper(), f"{code} ")


def format_amount(value, code="INR"):
    """"₹12,34,567.50" — Indian digit grouping for INR (which is how an INR tax
    invoice is read), plain thousands grouping for everything else."""
    amount = money(value)
    symbol = currency_symbol(code)
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    if (code or "INR").upper() == "INR" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        # The substitution is bound first rather than inlined into the f-string:
        # a backslash inside an f-string expression is only legal from Python
        # 3.12, and production runs 3.10.
        grouped = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head)
        whole = f"{grouped},{tail}"
    else:
        whole = f"{int(whole):,}"
    return f"{sign}{symbol}{whole}.{frac}"


# ---------- amount in words (Indian scale) ----------

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
         "Eighty", "Ninety")


def _under_hundred(n):
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")).strip()


def _under_thousand(n):
    if n < 100:
        return _under_hundred(n)
    rest = f" {_under_hundred(n % 100)}" if n % 100 else ""
    return f"{_ONES[n // 100]} Hundred{rest}"


def amount_in_words(value, code="INR"):
    """"Rupees One Lakh Twenty Thousand Only" — the line an Indian tax invoice
    is expected to carry. Empty for currencies we don't spell out, and the
    template simply omits the row."""
    names = CURRENCY_WORDS.get((code or "INR").upper())
    if names is None:
        return ""
    major_name, minor_name = names
    amount = money(abs(value))
    major, minor = int(amount), int((amount % 1) * 100)

    parts = []
    for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if major >= divisor:
            parts.append(f"{_under_thousand(major // divisor)} {label}")
            major %= divisor
    if major:
        parts.append(_under_thousand(major))

    words = " ".join(parts) or "Zero"
    text = f"{major_name} {words}"
    if minor:
        text += f" and {_under_hundred(minor)} {minor_name}"
    return f"{'Minus ' if value < 0 else ''}{text} Only"


# ---------- how GST is charged ----------

# What the invoice's "GST type" control can be set to. "auto" is the default and
# the only one that looks at the two states; the rest are deliberate overrides
# for the cases a state comparison can't see (export, SEZ, reverse charge).
GST_MODES = ("auto", "cgst_sgst", "igst", "single")
GST_MODE_LABELS = {
    "auto": "Automatic (from the two states)",
    "cgst_sgst": "CGST + SGST (same state)",
    "igst": "IGST (different state)",
    "single": "One combined GST line",
}


def resolve_gst_kind(mode, agency=None, client=None):
    """How the tax summary should be broken up: "split", "igst" or "gst".

    Automatic compares the agency's place of supply with the client's state —
    same state is an intra-state supply (CGST + SGST, half each), different is
    inter-state (IGST). When either state is unrecorded it falls back to one
    undivided GST line instead of picking: an invoice that splits the tax the
    wrong way is worse than one that doesn't split it at all.
    """
    if mode == "cgst_sgst":
        return "split"
    if mode == "igst":
        return "igst"
    if mode == "single":
        return "gst"
    agency_state = (getattr(agency, "state", "") or "").strip().casefold()
    client_state = (getattr(client, "state", "") or "").strip().casefold()
    if not agency_state or not client_state:
        return "gst"
    return "split" if agency_state == client_state else "igst"


def _tax_rows(group, kind, code):
    """The summary line(s) one rate bucket contributes.

    The split halves are derived from the bucket's tax rather than recomputed
    from the taxable amount, so CGST + SGST always adds back to exactly the GST
    charged. Recomputing each half independently can leave a stray paisa that
    makes the invoice fail to reconcile against itself.
    """
    rate, tax = group["rate"], group["tax"]
    if kind == "split":
        half_rate = f"{(rate / 2).normalize():f}"
        cgst = money(tax / 2)
        return [
            {"label": f"CGST @ {half_rate}%", "amount": cgst,
             "amount_display": format_amount(cgst, code)},
            {"label": f"SGST @ {half_rate}%", "amount": tax - cgst,
             "amount_display": format_amount(tax - cgst, code)},
        ]
    name = "IGST" if kind == "igst" else "GST"
    return [{"label": f"{name} @ {group['rate_display']}%", "amount": tax,
             "amount_display": format_amount(tax, code)}]


# ---------- totals ----------

def totals(items, code="INR", *, charge_gst=True, gst_kind="gst"):
    """Subtotal, tax broken up the way this invoice charges it, and the total.

    Grouping by rate matters: a real invoice can mix an 18% service line with a
    0% reimbursement, and the tax summary has to show them separately rather
    than as one blended number nobody can reconcile.

    `charge_gst=False` zeroes the tax without touching the rows — the rates
    stay stored, so ticking the box back on restores the same figures rather
    than making someone retype them.
    """
    subtotal = Decimal("0.00")
    groups = {}
    for item in items:
        subtotal += item.amount
        rate = money(item.tax_rate)
        bucket = groups.setdefault(rate, {"rate": rate,
                                          "taxable": Decimal("0.00"),
                                          "tax": Decimal("0.00")})
        bucket["taxable"] += item.amount
        bucket["tax"] += item.tax_amount

    tax_groups = sorted(groups.values(), key=lambda g: g["rate"]) if charge_gst else []
    tax_rows = []
    for group in tax_groups:
        group["taxable_display"] = format_amount(group["taxable"], code)
        group["tax_display"] = format_amount(group["tax"], code)
        # 18.00 reads as "18" on an invoice; 18.50 keeps its half point.
        group["rate_display"] = f"{group['rate'].normalize():f}"
        group["rows"] = _tax_rows(group, gst_kind, code)
        tax_rows += group["rows"]

    tax_total = sum((g["tax"] for g in tax_groups), Decimal("0.00"))
    grand_total = money(subtotal + tax_total)
    return {
        "subtotal": subtotal,
        "tax_groups": tax_groups,
        "tax_rows": tax_rows,
        "tax_total": tax_total,
        "grand_total": grand_total,
        "subtotal_display": format_amount(subtotal, code),
        "tax_total_display": format_amount(tax_total, code),
        "grand_total_display": format_amount(grand_total, code),
        "in_words": amount_in_words(grand_total, code),
        "currency": code,
        "symbol": currency_symbol(code),
        "charge_gst": charge_gst,
        "gst_kind": gst_kind,
    }


# ---------- posted rows ----------

def _at(values, index):
    return values[index] if index < len(values) else ""


def rows_from_post(post):
    """The items editor's parallel arrays, cleaned into row dicts.

    Deleting a row is simply not posting it — the editor drops it from the DOM
    and the next save replaces the whole set, so there is no separate delete
    endpoint to keep in sync. A row left completely blank is discarded the same
    way, which is what an accidentally-added empty row should do.

    A negative unit price is allowed on purpose: it is how a discount line is
    written, and the totals handle it correctly.
    """
    descriptions = post.getlist("item_description")
    quantities = post.getlist("item_quantity")
    units = post.getlist("item_unit")
    prices = post.getlist("item_unit_price")
    rates = post.getlist("item_tax_rate")

    rows = []
    for index, raw_description in enumerate(descriptions):
        description = (raw_description or "").strip()[:300]
        price = clamp(money(to_decimal(_at(prices, index))), -MAX_AMOUNT, MAX_AMOUNT)
        if not description and not price:
            continue
        rows.append({
            "description": description or "Item",
            "quantity": clamp(money(to_decimal(_at(quantities, index), Decimal("1"))),
                              Decimal("0"), MAX_QUANTITY),
            "unit": (_at(units, index) or "").strip()[:20],
            "unit_price": price,
            "tax_rate": clamp(money(to_decimal(_at(rates, index), DEFAULT_TAX_RATE)),
                              Decimal("0"), Decimal("100")),
            "order": len(rows),
        })
    return rows


def rows_from_ai(raw_items):
    """The AI's proposed rows, run through exactly the same cleaning as a human's.

    The model is an untrusted source of numbers like any other: whatever shape
    it returns, only description/quantity/unit/price/rate survive, clamped.
    """
    rows = []
    for entry in raw_items or []:
        if not isinstance(entry, dict):
            continue
        description = str(entry.get("description") or "").strip()[:300]
        price = clamp(money(to_decimal(entry.get("unit_price"))), -MAX_AMOUNT, MAX_AMOUNT)
        if not description and not price:
            continue
        rows.append({
            "description": description or "Item",
            "quantity": clamp(money(to_decimal(entry.get("quantity"), Decimal("1"))),
                              Decimal("0"), MAX_QUANTITY),
            "unit": str(entry.get("unit") or "").strip()[:20],
            "unit_price": price,
            "tax_rate": clamp(money(to_decimal(entry.get("tax_rate"), DEFAULT_TAX_RATE)),
                              Decimal("0"), Decimal("100")),
            "order": len(rows),
        })
    return rows


def replace_line_items(document, rows):
    """Swap the document's rows for `rows`. Whole-set replacement rather than a
    diff: the editor posts the table as it now stands, and reconciling row
    identities would buy nothing but a way to get them out of order."""
    from django.db import transaction
    from .models import LineItem

    with transaction.atomic():
        document.line_items.all().delete()
        LineItem.objects.bulk_create([
            LineItem(document=document, **row) for row in rows
        ])
    return document.line_items.all()


# ---------- rendering ----------

def parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return None


def build_context(document):
    """Everything the invoice body and the items editor both need."""
    from core.models import AgencySettings

    code = document.project.currency or "INR"
    items = list(document.line_items.all())
    # Per-row display strings, so neither the body template nor the editor has
    # to know how this app formats money.
    for item in items:
        item.qty_display = f"{item.quantity.normalize():f}"
        item.rate_display = f"{item.tax_rate.normalize():f}"
        item.unit_price_display = format_amount(item.unit_price, code)
        item.amount_display = format_amount(item.amount, code)
    meta = document.form_data or {}
    agency = AgencySettings.load()
    client = document.project.client
    # Documents created before the GST controls existed have neither key, and
    # a tax invoice that silently stopped charging GST would be the worse
    # default — so absent means "charge it, decide the split automatically".
    charge_gst = bool(meta.get("charge_gst", True))
    gst_mode = meta.get("gst_mode", "auto")
    if gst_mode not in GST_MODES:
        gst_mode = "auto"
    gst_kind = resolve_gst_kind(gst_mode, agency, client)
    return {
        "document": document,
        "doc_type": document.document_type,
        "project": document.project,
        "client": client,
        "agency": agency,
        "items": items,
        "totals": totals(items, code, charge_gst=charge_gst, gst_kind=gst_kind),
        "currency": code,
        "symbol": currency_symbol(code),
        "issue_date": parse_date(meta.get("issue_date")),
        "due_date": parse_date(meta.get("due_date")),
        "po_number": meta.get("po_number", ""),
        "notes": meta.get("notes", ""),
        "charge_gst": charge_gst,
        "gst_mode": gst_mode,
        "gst_kind": gst_kind,
        # For the editor: what "Automatic" works out to right now, and why.
        "gst_mode_choices": [(value, GST_MODE_LABELS[value]) for value in GST_MODES],
        "agency_state": agency.state,
        "client_state": client.state,
    }


def render_body(document):
    """The document's content_html, rebuilt from its rows.

    This is what gets stored on the DocumentVersion and handed to pdf.py, so
    the export path stays completely unchanged — a priced document is still
    just a document with some HTML in it.
    """
    return render_to_string("documents/_invoice_body.html", build_context(document))
