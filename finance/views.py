from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import require_perm
from core.models import log_activity
from projects.access import visible_projects
from projects.models import Project
from .models import Payment


def _form_context(project, payment=None, values=None, error=None):
    return {
        "project": project,
        "payment": payment,
        "types": Payment.Type.choices,
        "methods": Payment.Method.choices,
        # Optional invoice link: only this project's non-archived documents.
        "invoices": project.documents.filter(is_archived=False).select_related("document_type"),
        # Echoed back so a rejected submission doesn't lose what was typed.
        "posted": values,
        "error": error,
    }


def _payment_fields_from_post(request, project):
    """Validate and coerce the payment form. Returns (values, error).

    Money and dates used to be assigned straight from POST, so a missing field
    raised KeyError and a non-numeric amount blew up in the DB layer — both as
    a 500. The choice fields weren't checked against their enums either, so
    arbitrary strings were stored and echoed back through get_*_display().
    """
    amount_raw = (request.POST.get("amount") or "").strip()
    if not amount_raw:
        return None, "Enter the amount received."
    try:
        amount = Decimal(amount_raw)
    except (InvalidOperation, ValueError):
        return None, f"“{amount_raw}” isn't a valid amount."
    if amount <= 0:
        return None, "A payment amount must be greater than zero."

    received_raw = (request.POST.get("received_on") or "").strip()
    try:
        received_on = datetime.strptime(received_raw, "%Y-%m-%d").date()
    except ValueError:
        return None, "Enter the date received as YYYY-MM-DD."

    payment_type = request.POST.get("payment_type", "")
    method = request.POST.get("method", "")

    # An invoice link must point at THIS project — otherwise a payment could be
    # filed against another client's document.
    invoice_pk = (request.POST.get("invoice") or "").strip()
    invoice = None
    if invoice_pk:
        invoice = project.documents.filter(pk=invoice_pk).first()
        if invoice is None:
            return None, "That invoice doesn't belong to this project."

    return {
        "amount": amount,
        "received_on": received_on,
        "payment_type": (payment_type if payment_type in Payment.Type.values
                         else Payment.Type.MILESTONE),
        "method": (method if method in Payment.Method.values
                   else Payment.Method.OTHER),
        "reference": request.POST.get("reference", ""),
        "notes": request.POST.get("notes", ""),
        "invoice": invoice,
    }, None


@login_required
@require_perm("finance", "create")
def payment_create(request, project_pk):
    project = get_object_or_404(
        visible_projects(Project.objects.all(), request.user), pk=project_pk)
    if request.method == "POST":
        values, error = _payment_fields_from_post(request, project)
        if error:
            messages.error(request, error)
            return render(request, "finance/payment_form.html",
                          _form_context(project, values=request.POST, error=error))
        payment = Payment.objects.create(project=project, recorded_by=request.user,
                                         **values)
        log_activity(request.user, "recorded payment",
                     f"₹{payment.amount} for {project.name}")
        messages.success(request, f"Payment of ₹{payment.amount} recorded.")
        return redirect(project)
    return render(request, "finance/payment_form.html", _form_context(project))


@login_required
@require_perm("finance", "edit")
def payment_edit(request, pk):
    # Scoped through the project: a payment is money against somebody's
    # project, so reaching one you can't see the project for is the same leak.
    payment = get_object_or_404(
        Payment.objects.select_related("project").filter(
            project__in=visible_projects(Project.objects.all(), request.user)),
        pk=pk)
    project = payment.project
    if request.method == "POST":
        values, error = _payment_fields_from_post(request, project)
        if error:
            messages.error(request, error)
            return render(
                request, "finance/payment_form.html",
                _form_context(project, payment, values=request.POST, error=error))
        for field, value in values.items():
            setattr(payment, field, value)
        payment.save()
        log_activity(request.user, "updated payment",
                     f"₹{payment.amount} for {project.name}")
        messages.success(request, "Payment updated.")
        return redirect(project)
    return render(request, "finance/payment_form.html", _form_context(project, payment))


@login_required
@require_perm("finance", "delete")
@require_POST
def payment_delete(request, pk):
    # Scoped through the project: a payment is money against somebody's
    # project, so reaching one you can't see the project for is the same leak.
    payment = get_object_or_404(
        Payment.objects.select_related("project").filter(
            project__in=visible_projects(Project.objects.all(), request.user)),
        pk=pk)
    project = payment.project
    amount = payment.amount
    payment.delete()
    log_activity(request.user, "deleted payment", f"₹{amount} for {project.name}")
    messages.success(request, f"Payment of ₹{amount} deleted.")
    return redirect(project)
