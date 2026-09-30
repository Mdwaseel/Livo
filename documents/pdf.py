"""
PDF export for documents.

Engine-agnostic, same philosophy as the AI layer: prefers WeasyPrint when its
native libraries are present (typical on a Linux server), falls back to the
pure-Python xhtml2pdf so export works everywhere, including Windows dev boxes.

Documents with a visual Template are rendered image-based:
  page 1     -> cover_image, full bleed
  pages 2..n -> content_background behind the page, with the document's
                content_html flowing inside the drawn content region
                (content_x/y/width/height, percentages of A4)
  last page  -> back_image, full bleed — the constant closing page
Each of the three pages is skipped when its image is missing. Documents with
no template at all keep the clean built-in A4 shell branded from AgencySettings.
"""
import os
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.template import Context, Template as DjangoTemplate

from core.models import AgencySettings
from .services import resolve_template

A4_PT = (595.27, 841.89)   # xhtml2pdf works in points
A4_MM = (210.0, 297.0)     # WeasyPrint works in real units

DEFAULT_SHELL = """
<html><head><style>
@page { size: A4; margin: 2.2cm 1.8cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; color: #14161c; }
h1 { font-size: 20pt; color: {{ accent }}; margin: 0 0 6pt 0; }
h2 { font-size: 14pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 12pt; margin: 12pt 0 5pt 0; }
p { margin: 5pt 0; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; }
td, th { border: 1px solid #cccccc; padding: 6pt 8pt; font-size: 10.5pt; text-align: left; }
th { background-color: #f1f2f5; }
blockquote { margin: 8pt 0; padding: 4pt 10pt; color: #555555; }
.pdf-header { border-bottom: 2pt solid {{ accent }}; padding-bottom: 8pt; margin-bottom: 16pt; }
.pdf-footer { border-top: 1pt solid #dddddd; margin-top: 20pt; padding-top: 8pt;
              font-size: 9pt; color: #777777; }
</style></head><body>
<div class="pdf-header"><b style="font-size:14pt">{{ agency.agency_name }}</b>
{% if agency.tagline %}<br/><span style="font-size:9pt;color:#777777">{{ agency.tagline }}</span>{% endif %}
{% if document.number %}<br/><span style="font-size:9pt;color:#777777">{{ document.number }} · {{ document.updated_at|date:"d M Y" }}</span>{% endif %}
</div>
{{ content|safe }}
<div class="pdf-footer">{{ agency.agency_name }}{% if agency.gstin %} · GSTIN {{ agency.gstin }}{% endif %}{% if agency.email %} · {{ agency.email }}{% endif %}{% if agency.phone %} · {{ agency.phone }}{% endif %}{% if agency.website %} · {{ agency.website }}{% endif %}</div>
</body></html>
"""

# Typography for content flowing inside a template's drawn region.
CONTENT_CSS = """
body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; color: #14161c; }
h1 { font-size: 17pt; margin: 0 0 6pt 0; }
h2 { font-size: 13pt; margin: 12pt 0 5pt 0; }
h3 { font-size: 11.5pt; margin: 10pt 0 4pt 0; }
p { margin: 4pt 0; }
ul, ol { margin: 4pt 0 4pt 14pt; }
table { width: 100%; border-collapse: collapse; margin: 7pt 0; }
td, th { border: 1px solid #cccccc; padding: 5pt 7pt; font-size: 10pt; text-align: left; }
th { background-color: #f1f2f5; }
blockquote { margin: 7pt 0; padding: 3pt 9pt; color: #555555; }
"""


def template_for(document):
    """The page set this document prints, composed slot by slot.

    Deliberately NOT "its own template, else a default". Each of the three
    pages is resolved independently, so a document type whose own template
    carries only a cover still picks up the universal content background and
    the universal back page. See `services.ResolvedTemplate`.
    """
    return resolve_template(document.document_type, document.template)


def _content_html(document):
    return document.current_version.content_html if document.current_version else ""


# ---------- legacy shell (documents without a visual template) ----------

def build_html(document):
    agency = AgencySettings.load()
    context = Context({
        "content": _content_html(document),
        "document": document,
        "client": document.project.client,
        "project": document.project,
        "agency": agency,
        "accent": agency.primary_color or "#00795B",
    })
    return DjangoTemplate(DEFAULT_SHELL).render(context)


# ---------- visual rendering (cover / content region / back page) ----------

def build_visual_html_weasy(document, template):
    """CSS paged-media version for WeasyPrint: named pages + margins as region."""
    w_mm, h_mm = A4_MM
    left = template.content_x / 100 * w_mm
    top = template.content_y / 100 * h_mm
    right = w_mm - (template.content_x + template.content_width) / 100 * w_mm
    bottom = h_mm - (template.content_y + template.content_height) / 100 * h_mm

    bg = ""
    if template.content_background:
        # The content region is expressed as @page margins, and a page box's
        # background is positioned against its PADDING box -- which those
        # margins have already inset. `background-position: 0 0` therefore
        # started the letterhead at the top-left corner of the TEXT area, not
        # of the sheet: the design slid down by `top` and right by `left`, and
        # whatever fell past the far edge was cropped. On a letterhead with a
        # logo at 7% and a contact bar at 93%, that put the logo behind the
        # first line of text and cut the contact bar off the page entirely.
        #
        # Pulling the origin back by exactly the margins puts the image on the
        # sheet, which is where a full-bleed background belongs. `background-
        # origin: border-box` expresses the same intent more directly but is
        # not reliably honoured on page boxes, and this arithmetic is.
        bg = (f'background-image: url("{Path(template.content_background.path).as_uri()}"); '
              f"background-size: {w_mm}mm {h_mm}mm; "
              f"background-repeat: no-repeat; "
              f"background-position: -{left:.2f}mm -{top:.2f}mm;")
    pages = []
    if template.cover_image:
        pages.append(f'<div class="bleed"><img src="{Path(template.cover_image.path).as_uri()}"></div>')
    pages.append(f'<div class="content">{_content_html(document)}</div>')
    if template.back_image:
        pages.append(f'<div class="bleed"><img src="{Path(template.back_image.path).as_uri()}"></div>')

    return f"""<html><head><style>
@page {{ size: A4; margin: 0; }}
@page bleed {{ size: A4; margin: 0; }}
@page content {{ size: A4;
  margin: {top:.2f}mm {right:.2f}mm {bottom:.2f}mm {left:.2f}mm; {bg} }}
.bleed {{ page: bleed; width: {w_mm}mm; height: {h_mm}mm; page-break-after: always; }}
.bleed img {{ width: {w_mm}mm; height: {h_mm}mm; display: block; }}
.content {{ page: content; }}
{CONTENT_CSS}
</style></head><body>{''.join(pages)}</body></html>"""


def build_visual_html_pisa(document, template):
    """xhtml2pdf version: @page templates with @frame regions and full-page
    background images, switched with <pdf:nexttemplate>."""
    w_pt, h_pt = A4_PT
    left = template.content_x / 100 * w_pt
    top = template.content_y / 100 * h_pt
    width = template.content_width / 100 * w_pt
    height = template.content_height / 100 * h_pt

    full_frame = (f"@frame f {{ left: 0pt; top: 0pt; "
                  f"width: {w_pt:.2f}pt; height: {h_pt:.2f}pt; }}")
    content_frame = (f"@frame content {{ left: {left:.2f}pt; top: {top:.2f}pt; "
                     f"width: {width:.2f}pt; height: {height:.2f}pt; }}")
    bg = (f"background-image: url('{template.content_background.url}');"
          if template.content_background else "")

    css, body = [], []
    content_page = f"size: a4 portrait; margin: 0pt; {bg} {content_frame}"
    if template.cover_image:
        # unnamed @page renders page 1 (the cover); content pages are switched to
        css.append(f"@page {{ size: a4 portrait; margin: 0pt; "
                   f"background-image: url('{template.cover_image.url}'); {full_frame} }}")
        css.append(f"@page content_tpl {{ {content_page} }}")
        body += ['<pdf:nexttemplate name="content_tpl"/>', "<div>&nbsp;</div>",
                 "<pdf:nextpage/>"]
    else:
        css.append(f"@page {{ {content_page} }}")
    body.append(_content_html(document))
    if template.back_image:
        css.append(f"@page back_tpl {{ size: a4 portrait; margin: 0pt; "
                   f"background-image: url('{template.back_image.url}'); {full_frame} }}")
        body += ['<pdf:nexttemplate name="back_tpl"/>', "<pdf:nextpage/>",
                 "<div>&nbsp;</div>"]

    return (f"<html><head><style>{''.join(css)}{CONTENT_CSS}</style></head>"
            f"<body>{''.join(body)}</body></html>")


# ---------- engines ----------

def _weasyprint():
    """The WeasyPrint HTML class, or None when it (or its GTK libs) is missing."""
    try:
        from weasyprint import HTML
        return HTML
    except Exception:
        return None


def _link_callback(uri, rel):
    """Resolve media/static URLs to filesystem paths for xhtml2pdf."""
    for url, root in ((settings.MEDIA_URL, settings.MEDIA_ROOT),
                      (settings.STATIC_URL, settings.BASE_DIR / "static")):
        for candidate in (url, "/" + url.lstrip("/")):
            if candidate and uri.startswith(candidate):
                return os.path.join(root, uri[len(candidate):])
    return uri


def _absolutize_media(html):
    """Rewrite /media/ and /static/ srcs to file:// URIs so WeasyPrint
    resolves embedded images (its base_url is a filesystem path, under
    which absolute-URL srcs would otherwise dangle)."""
    for url, root in ((settings.MEDIA_URL, settings.MEDIA_ROOT),
                      (settings.STATIC_URL, settings.BASE_DIR / "static")):
        root_uri = Path(root).as_uri()
        for candidate in {url, "/" + str(url).lstrip("/")}:
            if candidate:
                html = html.replace(f'src="{candidate}', f'src="{root_uri}/')
                html = html.replace(f"src='{candidate}", f"src='{root_uri}/")
    return html


def _pisa_pdf(html):
    from xhtml2pdf import pisa
    # xhtml2pdf's built-in fonts have no ₹ glyph — substitute a text fallback.
    html = html.replace("₹", "Rs. ")
    buffer = BytesIO()
    result = pisa.CreatePDF(html, dest=buffer, link_callback=_link_callback)
    if result.err:
        raise RuntimeError("PDF rendering failed")
    return buffer.getvalue()


def html_to_pdf(html):
    HTML = _weasyprint()
    if HTML:
        try:
            return HTML(string=_absolutize_media(html),
                        base_url=str(settings.BASE_DIR)).write_pdf()
        except Exception:
            pass  # engine hiccup — use the pure-Python fallback
    return _pisa_pdf(html)


def render_document_pdf(document):
    """Full pipeline: content -> visual template pages (or branded shell) -> PDF."""
    template = template_for(document)
    if template and template.has_visual_pages:
        HTML = _weasyprint()
        if HTML:
            try:
                html = _absolutize_media(build_visual_html_weasy(document, template))
                return HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()
            except Exception:
                pass  # fall through to xhtml2pdf
        return _pisa_pdf(build_visual_html_pisa(document, template))
    return html_to_pdf(build_html(document))
