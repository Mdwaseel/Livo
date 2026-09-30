from django.core.management.base import BaseCommand
from documents.models import DocumentType

# Every document type takes ONE input: a free-text brief. The AI decides the
# structure (sections, tables, lists) from the brief plus client/project context.
BRIEF_FIELD = [
    {"name": "brief", "label": "Brief", "type": "textarea", "required": True,
     "placeholder": "Describe what this document should cover…"},
]

# Priced types ask for the few facts a real invoice carries that no brief
# reliably contains. The money itself is never a form field: it comes from the
# line-item rows, which the AI proposes and a human then edits.
PRICED_FIELDS = BRIEF_FIELD + [
    {"name": "issue_date", "label": "Date", "type": "date", "required": False,
     "hint": "Defaults to today."},
    {"name": "due_date", "label": "Payment due by", "type": "date", "required": False},
    {"name": "po_number", "label": "PO / reference", "type": "text", "required": False,
     "placeholder": "Client's purchase order number, if any"},
]

TYPES = [
    dict(name="Proposal", slug="proposal", category="PRE_DEAL", order=1,
         description="AI-drafted project proposal from a short brief.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write a professional web development proposal for {client_name} "
                    "for the project '{project_name}'. Brief: {brief}. "
                    "Include: understanding of the problem, our approach, scope of work, "
                    "timeline, and why {agency_name} is a good fit. "
                    "Professional, confident, not salesy.")),
    dict(name="Quotation", slug="quotation", category="PRE_DEAL", order=2,
         description="Itemised pricing with GST and totals, drafted from a brief.",
         form_schema=PRICED_FIELDS, is_priced=True,
         ai_prompt=("Quotation {number} from {agency_name} for {client_name}, "
                    "project '{project_name}'. Brief: {brief}. "
                    "Break the quoted work into one line per deliverable, each with "
                    "its per-unit price and GST rate, plus a short validity and "
                    "payment-terms note.")),
    dict(name="Contract", slug="contract", category="CLOSING", order=1,
         description="Editable service agreement.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Draft a web development service agreement between {agency_name} and "
                    "{client_name} for '{project_name}'. Brief: {brief}. "
                    "Include standard clauses: scope, payment, IP ownership, "
                    "confidentiality, revisions, termination. "
                    "Editable, plain professional legal English.")),
    dict(name="Onboarding Document", slug="onboarding", category="CLOSING", order=2,
         description="Welcome kit: contacts, process, what we need from the client.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write a client onboarding / welcome document for {client_name} "
                    "starting '{project_name}' with {agency_name}. Brief: {brief}. "
                    "Cover: points of contact, how we work, communication cadence, tools, "
                    "and what we need from the client (assets, logins, content). "
                    "Warm and clear.")),
    dict(name="Invoice", slug="invoice", category="CLOSING", order=3,
         description="GST tax invoice: editable line items, server-computed totals.",
         form_schema=PRICED_FIELDS, is_priced=True,
         ai_prompt=("Tax invoice {number} from {agency_name} to {client_name} for the "
                    "project '{project_name}'. Brief: {brief}. "
                    "List every billable line the brief describes, each with its "
                    "per-unit price and GST rate, plus a short payment-terms note.")),
    dict(name="SRS", slug="srs", category="DELIVERY", order=1,
         description="Software Requirement Specification.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write a Software Requirement Specification for '{project_name}' "
                    "({client_name}). Brief: {brief}. Include: introduction, functional "
                    "requirements, non-functional requirements, and system overview.")),
    dict(name="Meeting Minutes", slug="mom", category="DELIVERY", order=2,
         description="Structured MOM from raw notes.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Turn these meeting notes into structured minutes for '{project_name}' "
                    "({client_name}): {brief}. Sections: attendees, discussion points, "
                    "decisions, action items with owners.")),
    dict(name="Weekly Report", slug="weekly-report", category="DELIVERY", order=3,
         description="Progress report for the client.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write a weekly progress report for {client_name} on '{project_name}'. "
                    "Brief: {brief}. Cover what was completed, what is planned next, and "
                    "any blockers. Clear and concise.")),
    dict(name="QA Checklist", slug="qa-checklist", category="DELIVERY", order=4,
         description="Pre-delivery testing checklist.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Create a QA testing checklist for '{project_name}' ({client_name}). "
                    "Brief: {brief}. Group by category with checkable items.")),
    dict(name="Monthly Report", slug="monthly-report", category="DELIVERY", order=5,
         description="AI monthly progress report from the project's work log, "
                     "tasks and milestones, with proof photos.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write a professional monthly progress report in clean HTML for "
                    "{client_name}, project '{project_name}', covering {month_label}. "
                    "Verified facts about the month:\n{facts}\n"
                    "Optional team highlights to weave in: {brief}\n"
                    "Sections: a brief intro, work completed grouped by area, "
                    "milestones and impact, and a short next-month outlook. "
                    "Rules: use ONLY the facts provided, do NOT invent metrics or "
                    "numbers, no <html> or <body> tags, no markdown fences, and do "
                    "NOT include any <img> tags — photos are appended separately.")),
    dict(name="Maintenance Plan", slug="maintenance", category="CLOSEOUT", order=1,
         description="AMC / support plan with tiers.",
         form_schema=BRIEF_FIELD,
         ai_prompt=("Write an annual maintenance (AMC) and support plan from {agency_name} "
                    "for {client_name}'s '{project_name}'. Brief: {brief}. "
                    "Cover what's included, response times/SLAs, and pricing.")),
]


class Command(BaseCommand):
    help = "Seed the standard agency document types."

    def handle(self, *args, **opts):
        created = 0
        for t in TYPES:
            # is_priced spelled out for every type, so re-seeding also turns the
            # flag back OFF on a type that was flipped by hand.
            defaults = {"is_priced": False, **t}
            _, made = DocumentType.objects.update_or_create(
                slug=t["slug"], defaults=defaults)
            created += 1 if made else 0
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(TYPES)} document types ({created} new)."))
