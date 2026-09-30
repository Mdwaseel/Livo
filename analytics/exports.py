"""CSV and Excel writers.

Not in the file list the spec gave, but rendering a byte stream is neither a
metric (`services`) nor a query (`selectors`) nor a chart config (`charts`), and
folding it into `views` would put a zip-archive builder next to HTTP handling.
One extra module, named for what it does.

The `.xlsx` is written with `zipfile` and string templates rather than
openpyxl. That is a deliberate match for how this codebase already handles
third-party surface area — the Groq client is stdlib `urllib` for the same
reason. A spreadsheet with a header row and typed cells needs four small XML
parts; carrying a dependency (and its transitive tree) into a Python 3.14
environment that has already had wheel trouble buys nothing here.

Anything more elaborate — formulas, multiple sheets, charts inside the workbook
— is the point to reach for openpyxl instead.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from django.http import HttpResponse

CSV_CONTENT_TYPE = "text/csv; charset=utf-8"
XLSX_CONTENT_TYPE = ("application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# shared shaping
# ---------------------------------------------------------------------------

def _clean(value):
    """Normalise a cell for either writer."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _filename(stem, extension):
    return f"livo-{stem}-{date.today().isoformat()}.{extension}"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def csv_response(stem, headers, rows):
    response = HttpResponse(content_type=CSV_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="{_filename(stem, "csv")}"')
    # Excel decides a UTF-8 CSV is Latin-1 unless it finds a BOM, which turns
    # the ₹ in any currency column into mojibake on a default Windows install.
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_clean(cell) for cell in row])
    return response


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Two cell formats: 0 = default, 1 = bold (the header row).
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""


def _workbook(sheet_name):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""


def _column_ref(index):
    """0 → A, 25 → Z, 26 → AA. Needed because cells are addressed by name."""
    ref = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        ref = chr(65 + remainder) + ref
    return ref


def _cell(row_number, column_index, value, *, style=0):
    ref = f"{_column_ref(column_index)}{row_number}"
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    # Inline strings rather than a shared-strings part: one fewer XML part to
    # keep consistent, and these exports are read once and thrown away.
    text = escape(str(value))
    return (f'<c r="{ref}"{style_attr} t="inlineStr">'
            f"<is><t xml:space=\"preserve\">{text}</t></is></c>")


def _sheet(headers, rows):
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>']
    lines.append("<row r=\"1\">" + "".join(
        _cell(1, index, header, style=1)
        for index, header in enumerate(headers)) + "</row>")
    for offset, row in enumerate(rows, start=2):
        lines.append(f'<row r="{offset}">' + "".join(
            _cell(offset, index, _clean(cell))
            for index, cell in enumerate(row)) + "</row>")
    lines.append("</sheetData></worksheet>")
    return "".join(lines)


def xlsx_bytes(sheet_name, headers, rows):
    """A minimal but valid .xlsx. Excel, LibreOffice and Google Sheets all open
    it; the tests unzip it and parse the sheet XML to prove the bytes are real."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _workbook(sheet_name))
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", _sheet(headers, rows))
    return buffer.getvalue()


def xlsx_response(stem, sheet_name, headers, rows):
    response = HttpResponse(xlsx_bytes(sheet_name, headers, rows),
                            content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="{_filename(stem, "xlsx")}"')
    return response


# ---------------------------------------------------------------------------
# the exportable datasets
# ---------------------------------------------------------------------------

def employee_dataset(rows, *, include_finance=True):
    headers = ["Employee", "Department", "Total hours", "Avg daily hours",
               "Avg weekly hours", "Tasks completed", "Avg completion (days)",
               "Billable %", "Utilisation %", "Approval %", "Open tasks",
               "Overdue tasks"]
    data = [[
        r["user"].get_full_name() or r["user"].get_username(),
        r["department"].name if r["department"] else "",
        r["total_hours"], r["avg_daily_hours"], r["avg_weekly_hours"],
        r["tasks_completed"], r["avg_completion_days"], r["billable_percent"],
        r["utilization_percent"], r["approval_percent"],
        r["open_tasks"], r["overdue_tasks"],
    ] for r in rows]
    return headers, data


def project_dataset(rows, *, include_finance=True):
    headers = ["Project", "Client", "Estimated hours", "Actual hours",
               "Variance", "Completion %", "Milestones", "Overdue tasks",
               "Risk", "Health"]
    if include_finance:
        headers[9:9] = ["Budget", "Received", "Outstanding"]
    data = []
    for r in rows:
        row = [
            r["project"].name, r["client"].name,
            r["estimated_hours"], r["actual_hours"], r["hours_variance"],
            r["completion_percent"],
            f"{r['milestones_done']}/{r['milestones_total']}",
            r["overdue_tasks"],
        ]
        if include_finance:
            row += [r["budget"], r["received"], r["outstanding"]]
        row += [r["risk"]["level"], r["health"]]
        data.append(row)
    return headers, data


def client_dataset(rows, *, include_finance=True):
    headers = ["Client", "Projects", "Active projects", "Hours logged",
               "Documents", "Health"]
    if include_finance:
        headers[5:5] = ["Value", "Revenue", "Outstanding", "Collection %"]
    data = []
    for r in rows:
        row = [r["client"].name, r["project_count"], r["active_projects"],
               r["hours"], r["documents"]]
        if include_finance:
            row += [r["value"], r["revenue"], r["outstanding"],
                    r["collection_percent"]]
        row.append(r["health"])
        data.append(row)
    return headers, data


def department_dataset(rows, **_):
    headers = ["Department", "Headcount", "Hours", "Billable hours",
               "Billable %", "Utilisation %", "Tasks completed"]
    data = [[
        r["department"].name, r["headcount"], r["hours"], r["billable_hours"],
        r["billable_percent"], r["utilization_percent"], r["tasks_completed"],
    ] for r in rows]
    return headers, data


# Dataset key → (human label, builder). The view validates against these keys,
# so an unknown ?dataset= can't reach a getattr.
DATASETS = {
    "employees": ("Employees", employee_dataset),
    "projects": ("Projects", project_dataset),
    "clients": ("Clients", client_dataset),
    "departments": ("Departments", department_dataset),
}
