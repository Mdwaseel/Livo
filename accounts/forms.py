"""Forms for the sign-in, one-time-code and password-reset screens."""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, SetPasswordForm

User = get_user_model()

_TEXT_ATTRS = {"autocapitalize": "none", "autocorrect": "off", "spellcheck": "false"}


class LoginForm(AuthenticationForm):
    """Username + password. Rendered by hand in the template, so the only job
    here is sane widget attributes and a message that doesn't leak."""

    error_messages = {
        **AuthenticationForm.error_messages,
        # Deliberately identical whether the username exists or not — a
        # different wording for each turns the login page into a way to
        # enumerate who works here.
        "invalid_login": "Incorrect username or password.",
        "inactive": "Incorrect username or password.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({
            **_TEXT_ATTRS, "autocomplete": "username", "autofocus": True,
            "placeholder": "your username",
        })
        self.fields["password"].widget.attrs.update({
            "autocomplete": "current-password", "placeholder": "••••••••",
            "id": "id_password",
        })


class OTPForm(forms.Form):
    """The six-digit code from the email."""

    code = forms.CharField(
        label="Verification code",
        max_length=12,
        widget=forms.TextInput(attrs={
            **_TEXT_ATTRS,
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "autofocus": True,
            "placeholder": "······",
            "class": "otp-input",
        }),
    )

    def clean_code(self):
        # Users paste "123 456" or "123-456" out of the mail client.
        raw = "".join(ch for ch in self.cleaned_data["code"] if ch.isdigit())
        if not raw:
            raise forms.ValidationError("Enter the code from your email.")
        return raw


class BrandedPasswordResetForm(PasswordResetForm):
    """Django's reset form, restricted to accounts that can actually receive it.

    The base class already refuses to say whether an address matched — the view
    shows the same "check your inbox" page either way — so this only narrows
    *who* gets a mail, never what the requester is told.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs.update({
            **_TEXT_ATTRS, "autocomplete": "email", "autofocus": True,
            "placeholder": "you@livodigital.com",
        })

    def get_users(self, email):
        """Active users with a usable password, matched case-insensitively.

        Django's default already filters on is_active and has_usable_password;
        this override exists to key off the exact-but-case-insensitive address
        and to skip accounts with no password set at all.
        """
        return (
            user for user in User._default_manager.filter(
                email__iexact=email, is_active=True)
            if user.has_usable_password() and user.email
        )


class BrandedSetPasswordForm(SetPasswordForm):
    """The "choose a new password" step behind a reset link."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("new_password1", "new_password2"):
            self.fields[name].widget.attrs.update({
                "autocomplete": "new-password", "placeholder": "••••••••",
            })
        self.fields["new_password1"].widget.attrs["autofocus"] = True
