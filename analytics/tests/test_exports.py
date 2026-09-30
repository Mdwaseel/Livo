"""Export tests: the CSV opens cleanly in Excel, and the XLSX is a real
workbook rather than a renamed CSV."""
import io
import zipfile
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree

from django.test import SimpleTestCase

from analytics import exports

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class CleaningTests(SimpleTestCase):
    def test_none_becomes_empty_string(self):
        self.assertEqual(exports._clean(None), "")

    def test_decimal_becomes_float(self):
        self.assertEqual(exports._clean(Decimal("12.50")), 12.5)

    def test_booleans_read_as_words(self):
        self.assertEqual(exports._clean(True), "Yes")
        self.assertEqual(exports._clean(False), "No")

    def test_dates_are_iso(self):
        self.assertEqual(exports._clean(date(2026, 7, 26)), "2026-07-26")

    def test_column_reference_rolls_over_past_z(self):
        self.assertEqual(exports._column_ref(0), "A")
        self.assertEqual(exports._column_ref(25), "Z")
        self.assertEqual(exports._column_ref(26), "AA")
        self.assertEqual(exports._column_ref(27), "AB")


class CsvTests(SimpleTestCase):
    def response(self):
        return exports.csv_response(
            "projects", ["Name", "Value"], [["Website", Decimal("1000")]])

    def test_headers_and_rows(self):
        body = self.response().content.decode("utf-8-sig")
        self.assertIn("Name,Value", body)
        self.assertIn("Website,1000.0", body)

    def test_bom_is_present_so_excel_reads_utf8(self):
        """Without it Excel decodes as Latin-1 and ₹ turns to mojibake."""
        self.assertTrue(self.response().content.startswith(b"\xef\xbb\xbf"))

    def test_attachment_disposition_and_filename(self):
        response = self.response()
        disposition = response["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("livo-projects-", disposition)
        self.assertTrue(disposition.endswith('.csv"'))

    def test_none_cells_do_not_write_the_word_none(self):
        response = exports.csv_response("x", ["A"], [[None]])
        self.assertNotIn("None", response.content.decode("utf-8-sig"))


class XlsxTests(SimpleTestCase):
    def workbook(self, headers=None, rows=None):
        # `rows if rows is not None`, not `rows or` — an empty list is a valid
        # dataset and must not fall through to the default two rows.
        return exports.xlsx_bytes(
            "Projects",
            headers if headers is not None else ["Name", "Value"],
            rows if rows is not None else [["Website", Decimal("1000")],
                                           ["Branding", 50]])

    def test_output_is_a_zip_with_the_required_parts(self):
        archive = zipfile.ZipFile(io.BytesIO(self.workbook()))
        for part in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                     "xl/worksheets/sheet1.xml"):
            self.assertIn(part, archive.namelist())

    def test_every_part_is_well_formed_xml(self):
        archive = zipfile.ZipFile(io.BytesIO(self.workbook()))
        for name in archive.namelist():
            ElementTree.fromstring(archive.read(name))  # raises on malformed XML

    def test_sheet_holds_a_header_row_plus_one_row_per_record(self):
        archive = zipfile.ZipFile(io.BytesIO(self.workbook()))
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row")
        self.assertEqual(len(rows), 3)

    def test_numbers_are_stored_as_numbers_not_text(self):
        """A spreadsheet whose figures are strings can't be summed, which
        defeats the point of offering Excel at all."""
        archive = zipfile.ZipFile(io.BytesIO(self.workbook()))
        xml = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("<v>1000.0</v>", xml)
        self.assertNotIn('t="inlineStr"><is><t xml:space="preserve">1000.0', xml)

    def test_header_row_is_bold(self):
        archive = zipfile.ZipFile(io.BytesIO(self.workbook()))
        xml = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn('r="A1" s="1"', xml)

    def test_special_characters_are_escaped(self):
        """A client called "Smith & Sons <Ltd>" must not corrupt the file."""
        data = self.workbook(["Name"], [["Smith & Sons <Ltd>"]])
        archive = zipfile.ZipFile(io.BytesIO(data))
        xml = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("Smith &amp; Sons &lt;Ltd&gt;", xml)
        ElementTree.fromstring(xml)

    def test_sheet_name_is_escaped_too(self):
        data = exports.xlsx_bytes("R&D", ["A"], [["x"]])
        archive = zipfile.ZipFile(io.BytesIO(data))
        ElementTree.fromstring(archive.read("xl/workbook.xml"))

    def test_empty_dataset_still_produces_a_valid_workbook(self):
        archive = zipfile.ZipFile(io.BytesIO(self.workbook(["A"], [])))
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        self.assertEqual(
            len(sheet.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row")), 1)

    def test_response_carries_the_spreadsheet_content_type(self):
        response = exports.xlsx_response("projects", "Projects", ["A"], [["x"]])
        self.assertEqual(response["Content-Type"], exports.XLSX_CONTENT_TYPE)
        self.assertIn(".xlsx", response["Content-Disposition"])


class DatasetShapeTests(SimpleTestCase):
    """Header count must equal cell count, or the columns silently shift."""

    def test_project_dataset_columns_line_up_with_finance(self):
        row = {
            "project": type("P", (), {"name": "Website"})(),
            "client": type("C", (), {"name": "Acme"})(),
            "estimated_hours": Decimal("5"), "actual_hours": Decimal("6"),
            "hours_variance": Decimal("1"), "completion_percent": 50.0,
            "milestones_done": 1, "milestones_total": 2, "overdue_tasks": 0,
            "budget": Decimal("100"), "received": Decimal("40"),
            "outstanding": Decimal("60"),
            "risk": {"level": "LOW"}, "health": 90,
        }
        headers, data = exports.project_dataset([row], include_finance=True)
        self.assertEqual(len(headers), len(data[0]))

    def test_project_dataset_columns_line_up_without_finance(self):
        row = {
            "project": type("P", (), {"name": "Website"})(),
            "client": type("C", (), {"name": "Acme"})(),
            "estimated_hours": Decimal("5"), "actual_hours": Decimal("6"),
            "hours_variance": Decimal("1"), "completion_percent": 50.0,
            "milestones_done": 1, "milestones_total": 2, "overdue_tasks": 0,
            "risk": {"level": "LOW"}, "health": 90,
        }
        headers, data = exports.project_dataset([row], include_finance=False)
        self.assertEqual(len(headers), len(data[0]))
        self.assertNotIn("Budget", headers)

    def test_client_dataset_columns_line_up_both_ways(self):
        row = {"client": type("C", (), {"name": "Acme"})(), "project_count": 1,
               "active_projects": 1, "hours": Decimal("3"), "documents": 2,
               "value": Decimal("10"), "revenue": Decimal("4"),
               "outstanding": Decimal("6"), "collection_percent": 40.0,
               "health": 70}
        for flag in (True, False):
            headers, data = exports.client_dataset([row], include_finance=flag)
            self.assertEqual(len(headers), len(data[0]))

    def test_registry_covers_the_four_datasets(self):
        self.assertEqual(set(exports.DATASETS),
                         {"employees", "projects", "clients", "departments"})
