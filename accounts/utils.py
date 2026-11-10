import logging
from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
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
    if not token:
        return None, 'invalid'

    try:
        data = signing.loads(token, salt=EMAIL_VERIFICATION_SALT, max_age=max_age)
        user_id = data.get('user_id')
        email = data.get('email', '')

        user = User.objects.get(pk=user_id)
        if user.email.strip().lower() != email.strip().lower():
            return None, 'invalid'

        if user.email_verified:
            return user, 'already_verified'

        return user, 'valid'
    except signing.SignatureExpired:
        return None, 'expired'
    except (signing.BadSignature, User.DoesNotExist, Exception) as exc:
        logger.warning("Email verification token validation failed: %s", type(exc).__name__)
        return None, 'invalid'


def send_verification_email(request, user):
    """
    Constructs and sends an email verification link to the user's registered email.
    Returns True if sent successfully, or False if an email delivery error occurs.
    """
    if not user.email:
        return False

    try:
        token = generate_verification_token(user)
        verification_path = reverse('accounts:verify_email', kwargs={'token': token})
        verification_url = request.build_absolute_uri(verification_path)

        context = {
            'user': user,
            'verification_url': verification_url,
            'expiry_hours': 24,
        }

        subject = "Verify your MedAccess account email"
        text_content = render_to_string('accounts/email/verification_email.txt', context)
        html_content = render_to_string('accounts/email/verification_email.html', context)

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'MedAccess Healthcare <noreply@medaccess.com>')
        email_message = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[user.email],
        )
        email_message.attach_alternative(html_content, "text/html")
        email_message.send(fail_silently=False)
        return True
    except Exception as exc:
        logger.exception("Failed to deliver verification email")
        return False
