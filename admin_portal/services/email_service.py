import logging
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_pharmacy_approval_email(pharmacy_profile):
    """
    Sends an official account verification and approval confirmation email to the pharmacy.
    """
    recipient_email = getattr(pharmacy_profile, 'email', '') or getattr(pharmacy_profile.user, 'email', '')
    if not recipient_email:
        logger.warning(f"No email address found for pharmacy #{pharmacy_profile.id}")
        return False

    subject = "MedAccess — Pharmacy Account Approved & Activated"
    message = (
        f"Dear {pharmacy_profile.owner_name or pharmacy_profile.pharmacy_name},\n\n"
        f"Congratulations! Your pharmacy registration application for '{pharmacy_profile.pharmacy_name}' "
        f"(License: {pharmacy_profile.pharmacy_license_number}) has been verified and approved by the MedAccess administration team.\n\n"
        f"Your account is now fully active. You may log in to the MedAccess Pharmacy Portal using your registered username.\n\n"
        f"Portal Access: https://medaccess.com/accounts/login/\n\n"
        f"Best regards,\n"
        f"MedAccess Healthcare Administration Team"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@medaccess.com'),
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send pharmacy approval email to {recipient_email}: {e}")
        return False


def send_pharmacy_rejection_email(pharmacy_profile, rejection_reason=None):
    """
    Sends an official registration review and update request email to the rejected pharmacy.
    Communicates the rejection reason only if provided.
    """
    recipient_email = getattr(pharmacy_profile, 'email', '') or getattr(pharmacy_profile.user, 'email', '')
    if not recipient_email:
        logger.warning(f"No email address found for pharmacy #{pharmacy_profile.id}")
        return False

    reason_text = f"\nAdministrative Feedback:\n{rejection_reason}\n" if rejection_reason and rejection_reason.strip() else ""

    subject = "MedAccess — Pharmacy Registration Application Update"
    message = (
        f"Dear {pharmacy_profile.owner_name or pharmacy_profile.pharmacy_name},\n\n"
        f"Thank you for your interest in partnering with MedAccess. Our administrative compliance team "
        f"has reviewed your application for '{pharmacy_profile.pharmacy_name}' (License: {pharmacy_profile.pharmacy_license_number}).\n\n"
        f"At this stage, your application could not be verified with the submitted details.{reason_text}\n"
        f"You may log in to your account to review the feedback, update your credentials and store information, "
        f"and resubmit your application for verification.\n\n"
        f"Resubmit Application: https://medaccess.com/accounts/login/\n\n"
        f"If you have questions, please reply to this email or contact our support team.\n\n"
        f"Best regards,\n"
        f"MedAccess Compliance & Verification Team"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@medaccess.com'),
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send pharmacy rejection email to {recipient_email}: {e}")
        return False

