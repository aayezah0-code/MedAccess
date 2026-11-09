"""
Notification Service for MedAccess.

Provides reusable backend functionality for generating, validating, and persisting
notifications tied to PrescriptionRequest instances.
"""

from django.core.exceptions import ValidationError
from patient.models import Notification, PrescriptionRequest


def create_request_notification(request, recipient, notification_type, title, message, actor=None):
    """
    Creates, validates, and persists a Notification instance for a PrescriptionRequest.

    Parameters:
        request (PrescriptionRequest): The prescription request this notification is tied to.
        recipient (User): The target recipient user. Must be either the request's patient or pharmacy user.
        notification_type (str): Category choice from Notification.NOTIFICATION_TYPE_CHOICES.
        title (str): Concise notification title (max 200 characters, non-empty, non-whitespace).
        message (str): Body content of the notification (non-empty, non-whitespace).
        actor (User, optional): User initiating the event. If provided, must be either the request's
                                patient or pharmacy user. Pass None for system-generated alerts.

    Returns:
        Notification: The persisted Notification instance.

    Raises:
        ValidationError: If any argument fails validation, participant scoping, or model constraints.
    """
    # 1. Validate PrescriptionRequest instance
    if not isinstance(request, PrescriptionRequest):
        raise ValidationError({'request': 'A valid PrescriptionRequest instance is required.'})

    patient_user = getattr(getattr(request, 'patient', None), 'user', None)
    pharmacy_user = getattr(getattr(request, 'pharmacy', None), 'user', None)

    if not patient_user or not pharmacy_user:
        raise ValidationError({'request': 'The PrescriptionRequest must have associated patient and pharmacy users.'})

    allowed_participants = {patient_user.id: patient_user, pharmacy_user.id: pharmacy_user}

    # 2. Validate recipient
    if not recipient or not hasattr(recipient, 'id') or recipient.id not in allowed_participants:
        raise ValidationError({'recipient': 'Recipient must be either the request patient or pharmacy user.'})

    # 3. Validate actor (optional, but if provided must be a valid participant)
    if actor is not None:
        if not hasattr(actor, 'id') or actor.id not in allowed_participants:
            raise ValidationError({'actor': 'Actor must be either the request patient or pharmacy user.'})

    # 4. Validate notification_type against model choices
    valid_types = {choice[0] for choice in Notification.NOTIFICATION_TYPE_CHOICES}
    if notification_type not in valid_types:
        raise ValidationError({'notification_type': f"Invalid notification type '{notification_type}'."})

    # 5. Validate title
    if not isinstance(title, str) or not title.strip():
        raise ValidationError({'title': 'Title cannot be blank or whitespace-only.'})
    if len(title) > 200:
        raise ValidationError({'title': 'Title cannot exceed 200 characters.'})

    # 6. Validate message
    if not isinstance(message, str) or not message.strip():
        raise ValidationError({'message': 'Message cannot be blank or whitespace-only.'})

    # 7. Construct Notification instance
    notification = Notification(
        prescription_request=request,
        recipient=recipient,
        actor=actor,
        notification_type=notification_type,
        title=title,
        message=message,
    )

    # 8. Enforce model-level full_clean()
    notification.full_clean()

    # 9. Persist and return
    notification.save()
    return notification

