import logging
from django.conf import settings
from django.core import signing
import json
import os
import urllib.request
import urllib.error
from django.template.loader import render_to_string
from django.urls import reverse

from accounts.models import User

logger = logging.getLogger(__name__)

EMAIL_VERIFICATION_SALT = 'medaccess-email-verification-v1'
EMAIL_VERIFICATION_MAX_AGE = 86400  # 24 hours in seconds


def generate_verification_token(user):
    """
    Generates a secure, cryptographically signed, timestamped token
    encoding the user's primary key and email address.
    """
    payload = {
        'user_id': user.pk,
        'email': user.email.strip().lower(),
    }
    return signing.dumps(payload, salt=EMAIL_VERIFICATION_SALT)


def validate_verification_token(token, max_age=EMAIL_VERIFICATION_MAX_AGE):
    """
    Validates the verification token.
    Returns a tuple of (user, status):
      - (user, 'valid'): Valid token for an unverified account.
      - (user, 'already_verified'): Valid token, but user is already verified.
      - (None, 'expired'): Token was issued more than max_age seconds ago.
      - (None, 'invalid'): Token was forged, altered, corrupted, or user does not exist.
    """
    cleaned_token = str(token or '').strip().strip('\'"/')
    if not cleaned_token:
        return None, 'invalid'

    try:
        data = signing.loads(cleaned_token, salt=EMAIL_VERIFICATION_SALT, max_age=max_age)
        user_id = data.get('user_id')
        email = data.get('email', '')

        user = User.objects.get(pk=user_id)
        if user.email.strip().lower() != email.strip().lower():
            return None, 'invalid'

        if user.email_verified:
            return user, 'already_verified'

        return user, 'valid'
    except signing.SignatureExpired:
        logger.info("Verification token validation: signature expired")
        return None, 'expired'
    except (signing.BadSignature, User.DoesNotExist, Exception) as exc:
        logger.warning("Email verification token validation failed: %s", type(exc).__name__)
        return None, 'invalid'



def send_verification_email(request, user):
    """
    Sends an email verification link using Brevo HTTPS API.
    Returns True when Brevo accepts the request, otherwise False.
    """

    if not user or not user.email:
        logger.warning("Cannot send verification email: missing user/email")
        return False

    api_key = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("BREVO_FROM_EMAIL", "").strip()
    from_name = os.getenv(
        "BREVO_FROM_NAME", "MedAccess Healthcare"
    ).strip()

    if not api_key or not from_email:
        logger.error("Brevo configuration is incomplete")
        return False

    try:
        token = generate_verification_token(user)
        verification_path = reverse(
            "accounts:verify_email",
            kwargs={"token": token}
        )

        if request is not None:
            verification_url = request.build_absolute_uri(
                verification_path
            )
        else:
            base_url = "https://medaccess-dg1f.onrender.com"
            verification_url = f"{base_url}{verification_path}"

        context = {
            "user": user,
            "verification_url": verification_url,
            "expiry_hours": 24,
        }

        subject = "Verify your MedAccess account email"

        text_content = render_to_string(
            "accounts/email/verification_email.txt",
            context
        )

        html_content = render_to_string(
            "accounts/email/verification_email.html",
            context
        )

        payload = {
            "sender": {
                "name": from_name,
                "email": from_email,
            },
            "to": [
                {
                    "email": user.email,
                    "name": user.first_name or user.username,
                }
            ],
            "subject": subject,
            "textContent": text_content,
            "htmlContent": html_content,
        }

        data = json.dumps(payload).encode("utf-8")

        api_request = urllib.request.Request(
            url="https://api.brevo.com/v3/smtp/email",
            data=data,
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(
            api_request, timeout=15
        ) as response:
            response_body = response.read().decode("utf-8")
            result = json.loads(response_body)

            message_id = result.get("messageId")

            if response.status == 201 and message_id:
                logger.info(
                    "Brevo accepted verification email for user pk=%s",
                    user.pk
                )
                return True

            logger.error(
                "Brevo returned unexpected response for user pk=%s",
                user.pk
            )
            return False

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "Brevo API rejected email for user pk=%s. HTTP status=%s, response=%s",
            getattr(user, "pk", "unknown"),
            exc.code,
            error_body[:1000],
        )
        return False

    except Exception as exc:
        logger.exception(
            "Brevo email request failed for user pk=%s: %s",
            getattr(user, "pk", "unknown"),
            type(exc).__name__,
        )
        return False
