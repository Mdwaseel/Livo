"""
AI content generation on Groq Cloud with key rotation.

Each key in GROQ_API_KEYS is tried in order; when all fail (or none is set)
the caller-supplied fallback / offline placeholder is used. Calls go through
Groq's OpenAI-compatible REST endpoint with stdlib urllib, so no SDK installs
are needed. Configure keys in .env; the app stays fully usable with no keys.
"""
import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT = 90

_SYSTEM = (
    "You are a document writer for a web development agency. "
    "Produce clean, professional HTML (headings, paragraphs, tables, lists) "
    "with no markdown fences. Content only, no <html> or <body> tags. "
    "Never invent monetary figures: when the prompt supplies computed amounts "
    "(subtotal, GST, total), reproduce them exactly."
)


# Priced documents (invoice, quotation) take a different route: the model is
# asked for DATA, not prose. It reads rows out of the brief and documents.pricing
# does every sum, because "show the arithmetic consistently" is the one
# instruction a language model reliably ignores.
_PRICING_SYSTEM = """You turn a short brief for an agency invoice or quotation \
into structured data. Reply with ONE JSON object and nothing else — no prose, \
no markdown fences, no explanation. Shape:
{"items": [{"description": "Landing page design", "quantity": 2, \
"unit": "pages", "unit_price": 25000, "tax_rate": 18}], \
"notes": "50% advance, balance on delivery."}
Rules:
- unit_price is the price for ONE unit, excluding tax, as a plain number: no \
currency symbol, no thousands separators, no arithmetic expressions.
- If the brief gives a bundled price for several units, divide it out.
- Do NOT compute subtotal, tax or total. They are calculated elsewhere and \
anything you add will be discarded.
- Use only amounts the brief actually states. Invent no figures.
- tax_rate is a GST percentage; use 18 unless the brief says otherwise.
- notes is one short plain-text paragraph of payment terms or caveats.
- If the brief names no priced work at all, return an empty items list."""


# A placeholder is exactly {identifier}. Anything else a prompt happens to
# contain — CSS like `{ color: red }`, a JSON example, a stray brace — is left
# alone. str.format_map can't do this: it parses the WHOLE string as a format
# spec, so one literal brace raises ValueError and takes the request with it.
# ai_prompt is admin-editable free text, so that was reachable from the UI.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _fill(template, context):
    """Substitute known {placeholders}; leave unknown ones and literal braces
    exactly as written."""
    def replace(match):
        value = context.get(match.group(1), match.group(0))
        return "" if value is None else str(value)
    return _PLACEHOLDER.sub(replace, template or "")


def build_prompt(document_type, form_data, client, project, extra_context=None):
    """Fill the DocumentType's prompt template with the form answers + context."""
    from core.models import AgencySettings

    context = {
        "client_name": client.name,
        "client_city": client.city,
        "client_gstin": getattr(client, "gstin", ""),
        "project_name": project.name,
        "project_type": project.project_type,
        "agency_name": AgencySettings.load().agency_name,
        **{k: v for k, v in (form_data or {}).items()},
        **(extra_context or {}),
    }
    prompt = document_type.ai_prompt or (
        f"Write a professional {document_type.name} for {{client_name}}."
    )
    return _fill(prompt, context)


def _providers():
    """Yield (label, url, key, model) for each configured Groq key, in order."""
    keys = settings.GROQ_API_KEYS
    for i, key in enumerate(keys, start=1):
        label = "Groq" if len(keys) == 1 else f"Groq #{i}"
        yield label, GROQ_URL, key, settings.GROQ_MODEL


def generate_content(document_type, form_data, client, project,
                     extra_context=None, fallback_html=None):
    """Return HTML for the document. Tries each Groq key in order;
    if every key fails (or none is configured) returns the fallback."""
    prompt = build_prompt(document_type, form_data, client, project, extra_context)
    errors = []

    for label, url, key, model in _providers():
        try:
            return _chat(url, key, model, prompt)
        except Exception as e:
            errors.append(f"{label}: {e}")
            logger.warning("AI provider %s failed, trying next: %s", label, e)

    if fallback_html:
        return fallback_html
    if errors:
        return _placeholder(document_type,
                            f"[All AI providers failed — {'; '.join(errors)}]\n\n{prompt}")
    return _placeholder(document_type, prompt)


def extract_priced_document(document_type, form_data, client, project,
                            extra_context=None):
    """Brief -> {"items": [row, ...], "notes": str} for a priced document.

    Returns empty structures rather than raising when there is no API key, the
    provider is down, or the reply isn't parseable JSON. That matters: the
    invoice must still be created with its number and its editable table, so a
    human can type the rows in. A failed AI call degrades the draft, it does
    not lose the document.
    """
    prompt = build_prompt(document_type, form_data, client, project, extra_context)
    for label, url, key, model in _providers():
        try:
            data = _parse_json_object(_chat(url, key, model, prompt,
                                            system=_PRICING_SYSTEM))
        except Exception as e:
            logger.warning("AI provider %s failed on line items: %s", label, e)
            continue
        if data is not None:
            items = data.get("items")
            return {"items": items if isinstance(items, list) else [],
                    "notes": str(data.get("notes") or "").strip()}
        logger.warning("AI provider %s returned unparseable line-item JSON", label)
    return {"items": [], "notes": ""}


def _parse_json_object(text):
    """The first complete JSON object in the reply, or None.

    Models garnish JSON with a lead-in sentence often enough that scanning from
    the first brace to the last is worth the four lines it costs.
    """
    text = _clean(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _chat(url, api_key, model, prompt, system=None):
    """One OpenAI-compatible chat completion via stdlib urllib."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system or _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4000,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # Cloudflare in front of the API rejects urllib's default UA (error 1010)
        "User-Agent": "LivoOS/1.0 (+https://livodigital.com)",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {detail}".strip()) from e
    return _clean(data["choices"][0]["message"]["content"])


def _clean(text):
    """Strip markdown code fences some models wrap HTML in."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _placeholder(document_type, prompt):
    name = document_type.name if document_type else "Document"
    return (
        f"<h1>{name}</h1>"
        "<p><em>Placeholder draft — set GROQ_API_KEYS in .env "
        "to generate real content.</em></p><hr>"
        f"<p><strong>Prompt that will be sent to the AI:</strong></p><blockquote>{prompt}"
        "</blockquote>"
    )


# ---------------------------------------------------------------------------
# executive brief
# ---------------------------------------------------------------------------

_BRIEF_SYSTEM = (
    "You are a business analyst writing the morning brief for the leadership "
    "of a small agency. You are given a JSON object of figures ALREADY "
    "COMPUTED from the company's database. "
    "Write 3 to 5 short sentences of plain prose interpreting them. "
    "Rules you must not break: use ONLY the figures given; never invent a "
    "number, a name, a percentage or a trend; if the data is thin, say that "
    "plainly rather than padding. No markdown, no headings, no bullet points, "
    "no preamble such as 'Here is'. Return the sentences and nothing else."
)

# Well under the model's limit, and short on purpose: this is a brief, and a
# model given room for six paragraphs will use it.
_BRIEF_MAX_CHARS = 900


def business_brief(facts):
    """Prose interpretation of already-computed figures, or None.

    `None` is a first-class result, not an error path. Every caller renders the
    arithmetic brief regardless and treats this as an optional extra, so a
    missing key, a rate limit, a timeout or a network blip costs the page a
    paragraph rather than a page. That is also why nothing here raises: the one
    thing a dashboard must never do is fail to load because a third party is
    having a bad morning.

    The model is handed numbers and asked to interpret them — it is never asked
    to produce a figure, because the figures are the part that has to be right
    and `analytics.executive` has already worked them out.
    """
    if not settings.GROQ_API_KEYS:
        return None

    prompt = (
        "Here are today's figures for the business, computed from the "
        "database. Interpret them for the leadership team.\n\n"
        + json.dumps(facts, indent=1, default=str)
    )
    for label, url, key, model in _providers():
        try:
            text = _chat(url, key, model, prompt, system=_BRIEF_SYSTEM)
        except Exception as e:
            logger.warning("AI brief provider %s failed: %s", label, e)
            continue
        text = (text or "").strip()
        if text:
            return text[:_BRIEF_MAX_CHARS]
    return None
