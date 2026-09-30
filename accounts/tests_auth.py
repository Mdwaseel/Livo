"""Two-step sign-in, throttling, and the password-reset flow."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import LoginOTP, RateLimit

User = get_user_model()

PASSWORD = "correct-horse-battery-42"


def code_on_screen(client):
    """The six digits exactly as the verification page displays them."""
    response = client.get(reverse("accounts:login_otp"))
    return response.context["display_code"]


@override_settings(OTP_LOGIN_REQUIRED=True)
class LoginFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ana", email="ana@livodigital.com", password=PASSWORD)
        self.login_url = reverse("accounts:login")
        self.otp_url = reverse("accounts:login_otp")

    # --- step 1 ---

    def test_correct_password_does_not_sign_you_in(self):
        response = self.client.post(
            self.login_url, {"username": "ana", "password": PASSWORD})
        self.assertRedirects(response, self.otp_url)
        # The whole point: a stolen password alone reaches nothing.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_is_shown_on_screen_and_not_emailed(self):
        self.client.post(self.login_url, {"username": "ana", "password": PASSWORD})
        response = self.client.get(self.otp_url)
        code = response.context["display_code"]
        self.assertRegex(code, r"^\d{6}$")
        self.assertContains(response, code)
        self.assertEqual(len(mail.outbox), 0)

    def test_dashboard_is_unreachable_between_the_two_steps(self):
        self.client.post(self.login_url, {"username": "ana", "password": PASSWORD})
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_wrong_password_issues_no_code(self):
        self.client.post(self.login_url, {"username": "ana", "password": "nope"})
        self.assertFalse(LoginOTP.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_message_is_identical_for_unknown_user_and_wrong_password(self):
        unknown = self.client.post(
            self.login_url, {"username": "ghost", "password": "nope"})
        wrong = self.client.post(
            self.login_url, {"username": "ana", "password": "nope"})
        self.assertContains(unknown, "Incorrect username or password")
        self.assertContains(wrong, "Incorrect username or password")

    # --- step 2 ---

    def _reach_otp_step(self):
        self.client.post(self.login_url, {"username": "ana", "password": PASSWORD})
        return code_on_screen(self.client)

    def test_correct_code_signs_you_in(self):
        code = self._reach_otp_step()
        response = self.client.post(self.otp_url, {"code": code})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_code_with_spaces_is_accepted(self):
        code = self._reach_otp_step()
        spaced = f"{code[:3]} {code[3:]}"
        self.client.post(self.otp_url, {"code": spaced})
        self.assertIn("_auth_user_id", self.client.session)

    def test_wrong_code_does_not_sign_you_in(self):
        code = self._reach_otp_step()
        wrong = "000000" if code != "000000" else "111111"
        response = self.client.post(self.otp_url, {"code": wrong})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "attempts left")

    def test_a_code_works_only_once(self):
        code = self._reach_otp_step()
        self.client.post(self.otp_url, {"code": code})
        self.client.logout()
        self.client.post(self.login_url, {"username": "ana", "password": PASSWORD})
        response = self.client.post(self.otp_url, {"code": code})
        # The replayed code belongs to a consumed row; the live one differs.
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(response.status_code, 200)

    def test_expired_code_is_refused(self):
        code = self._reach_otp_step()
        LoginOTP.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.client.post(self.otp_url, {"code": code})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "expired")

    def test_code_is_not_stored_in_plaintext(self):
        code = self._reach_otp_step()
        otp = LoginOTP.objects.get()
        self.assertNotEqual(otp.code_hash, code)
        self.assertNotIn(code, otp.code_hash)
        self.assertTrue(otp.matches(code))

    def test_code_is_not_shown_once_expired(self):
        self._reach_otp_step()
        LoginOTP.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(code_on_screen(self.client), "")

    def test_new_code_replaces_the_one_on_screen(self):
        first = self._reach_otp_step()
        LoginOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.client.post(reverse("accounts:login_otp_resend"))
        second = code_on_screen(self.client)
        self.assertRegex(second, r"^\d{6}$")
        self.client.post(self.otp_url, {"code": second})
        self.assertIn("_auth_user_id", self.client.session)

    def test_code_from_another_browser_is_refused(self):
        """The session binding: holding the code isn't enough without the
        session it was issued to."""
        code = self._reach_otp_step()
        other = self.client_class()
        # Give the other client its own half-authenticated state.
        other.post(self.login_url, {"username": "ana", "password": PASSWORD})
        response = other.post(self.otp_url, {"code": code})
        self.assertNotIn("_auth_user_id", other.session)
        self.assertEqual(response.status_code, 200)

    def test_otp_page_without_a_pending_login_bounces(self):
        response = self.client.get(self.otp_url)
        self.assertRedirects(response, self.login_url)

    @override_settings(OTP_MAX_ATTEMPTS=3)
    def test_code_guesses_are_capped(self):
        code = self._reach_otp_step()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(3):
            self.client.post(self.otp_url, {"code": wrong})
        # Even the right code is refused once the attempts are spent.
        self.client.post(self.otp_url, {"code": code})
        self.assertNotIn("_auth_user_id", self.client.session)

    # --- no email needed any more ---

    def test_user_without_email_can_sign_in(self):
        User.objects.create_user(username="noemail", password=PASSWORD)
        response = self.client.post(
            self.login_url, {"username": "noemail", "password": PASSWORD})
        self.assertRedirects(response, self.otp_url)
        self.client.post(self.otp_url, {"code": code_on_screen(self.client)})
        self.assertIn("_auth_user_id", self.client.session)


@override_settings(OTP_LOGIN_REQUIRED=True, LOGIN_MAX_FAILURES=3,
                   LOGIN_LOCKOUT_SECONDS=900)
class ThrottleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ana", email="ana@livodigital.com", password=PASSWORD)
        self.login_url = reverse("accounts:login")

    def test_repeated_failures_lock_the_account(self):
        for _ in range(3):
            self.client.post(self.login_url, {"username": "ana", "password": "nope"})
        response = self.client.post(
            self.login_url, {"username": "ana", "password": "nope"})
        self.assertContains(response, "Too many failed attempts")

    def test_lockout_blocks_the_real_password_too(self):
        """Otherwise the lock is decorative — an attacker who eventually guesses
        right would still get in."""
        for _ in range(3):
            self.client.post(self.login_url, {"username": "ana", "password": "nope"})
        response = self.client.post(
            self.login_url, {"username": "ana", "password": PASSWORD})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "Too many failed attempts")
        self.assertFalse(LoginOTP.objects.exists())

    def test_success_clears_the_counter(self):
        self.client.post(self.login_url, {"username": "ana", "password": "nope"})
        self.client.post(self.login_url, {"username": "ana", "password": PASSWORD})
        self.assertFalse(RateLimit.objects.filter(key="pw:ana").exists())

    def test_counter_is_case_insensitive_on_the_username(self):
        for name in ("ana", "ANA", "Ana"):
            self.client.post(self.login_url, {"username": name, "password": "nope"})
        response = self.client.post(
            self.login_url, {"username": "ana", "password": "nope"})
        self.assertContains(response, "Too many failed attempts")


class PasswordResetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ana", email="ana@livodigital.com", password=PASSWORD)

    def test_reset_email_is_sent_and_link_works(self):
        self.client.post(reverse("accounts:password_reset"),
                         {"email": "ana@livodigital.com"})
        self.assertEqual(len(mail.outbox), 1)

        body = mail.outbox[0].body
        path = [line for line in body.splitlines() if "/password/reset/" in line][0].strip()
        # Following it lands on the "set a new password" form via Django's
        # one-shot redirect to the set-password token URL.
        response = self.client.get(path, follow=True)
        self.assertContains(response, "New password")

        new_password = "brand-new-passphrase-77"
        self.client.post(response.redirect_chain[-1][0] if response.redirect_chain
                         else path,
                         {"new_password1": new_password, "new_password2": new_password})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))

    def test_unknown_address_gets_the_same_page_and_no_mail(self):
        response = self.client.post(reverse("accounts:password_reset"),
                                    {"email": "nobody@example.com"}, follow=True)
        self.assertContains(response, "Check your inbox")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(LOGIN_MAX_FAILURES=2)
    def test_reset_requests_are_rate_limited(self):
        for _ in range(4):
            self.client.post(reverse("accounts:password_reset"),
                             {"email": "ana@livodigital.com"})
        # Two get through, the rest are swallowed silently.
        self.assertEqual(len(mail.outbox), 2)

    def test_reset_invalidates_a_pending_login_code(self):
        self.client.post(reverse("accounts:login"),
                         {"username": "ana", "password": PASSWORD})
        self.assertTrue(LoginOTP.objects.filter(consumed_at__isnull=True).exists())

        mail.outbox.clear()
        self.client.post(reverse("accounts:password_reset"),
                         {"email": "ana@livodigital.com"})
        path = [line for line in mail.outbox[0].body.splitlines()
                if "/password/reset/" in line][0].strip()
        response = self.client.get(path, follow=True)
        new_password = "brand-new-passphrase-77"
        self.client.post(response.redirect_chain[-1][0],
                         {"new_password1": new_password, "new_password2": new_password})
        self.assertFalse(LoginOTP.objects.filter(consumed_at__isnull=True).exists())


@override_settings(OTP_LOGIN_REQUIRED=True)
class LogoutTests(TestCase):
    def test_logout_requires_post(self):
        User.objects.create_user(username="ana", email="a@b.com", password=PASSWORD)
        response = self.client.get(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 405)
