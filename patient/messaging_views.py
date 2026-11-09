"""
Shared Participant-Aware Messaging Views for MedAccess.

Provides backend API endpoints for retrieving and sending messages associated
with a specific PrescriptionRequest between the requesting patient and the
selected approved pharmacy.
"""

import json
import logging

from django.db import transaction
from django.http import JsonResponse, Http404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from patient.models import PrescriptionRequest, RequestMessage
from patient.services.notification_service import create_request_notification

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def request_messages_api(request, pk):
    """
    Shared participant-aware endpoint for PrescriptionRequest messaging.

    GET /patient/requests/<pk>/messages/
    GET /pharmacy/requests/<pk>/messages/
        Retrieves messages for the specified request.
        Optional query parameter:
            ?after_id=<int>: Returns only messages with id > after_id.
        Limits results to at most 50 messages, ordered chronologically.
        Automatically marks unread messages addressed to the current user as read.

    POST /patient/requests/<pk>/messages/
    POST /pharmacy/requests/<pk>/messages/
        Sends a new message from the authenticated participant to the other participant.
        Accepts:
            message_text (str): 1 to 1000 characters, non-empty, non-whitespace.
        Enforces writable status check (PENDING, ACCEPTED, PROCESSING, READY_FOR_PICKUP).
        Rejects messages for archived requests (COMPLETED, REJECTED, CANCELLED).
        Derives sender and recipient exclusively server-side.
        Creates a NEW_MESSAGE in-app notification for the recipient within the same atomic transaction.
    """
    # 1. Enforce authentication
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required.'}, status=401)

    # 2. Fetch PrescriptionRequest with related participants
    try:
        presc_request = (
            PrescriptionRequest.objects.select_related(
                'patient__user', 'pharmacy__user', 'pharmacy'
            ).get(pk=pk)
        )
    except PrescriptionRequest.DoesNotExist:
        raise Http404("Prescription request not found.")

    patient_user = presc_request.patient.user
    pharmacy_user = presc_request.pharmacy.user

    # 3. Security: Participant Access Control
    if request.user.id not in (patient_user.id, pharmacy_user.id):
        raise Http404("Prescription request not found.")

    # 4. If participant is pharmacy, verify approved status
    if request.user.id == pharmacy_user.id:
        pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
        if not pharmacy_profile or pharmacy_profile.approval_status != 'APPROVED':
            return JsonResponse({'error': 'Pharmacy account is not approved.'}, status=403)

    is_patient = (request.user.id == patient_user.id)
    sender = patient_user if is_patient else pharmacy_user
    recipient = pharmacy_user if is_patient else patient_user

    # =========================================================================
    # GET: Retrieve Messages
    # =========================================================================
    if request.method == 'GET':
        messages_qs = presc_request.messages.select_related('sender').order_by('created_at', 'id')

        # Support incremental polling
        after_id = request.GET.get('after_id', '').strip()
        if after_id.isdigit():
            messages_qs = messages_qs.filter(id__gt=int(after_id))

        # Max 50 messages per retrieval
        messages_list = list(messages_qs[:50])

        # Mark unread messages addressed to the current user as read
        unread_ids = [m.id for m in messages_list if m.recipient_id == request.user.id and not m.is_read]
        if unread_ids:
            now = timezone.now()
            RequestMessage.objects.filter(id__in=unread_ids).update(is_read=True, read_at=now)
            for m in messages_list:
                if m.id in unread_ids:
                    m.is_read = True
                    m.read_at = now

        # Serialize messages
        data = []
        for m in messages_list:
            if m.sender_id == patient_user.id:
                sender_display = patient_user.get_full_name() or patient_user.username
            else:
                sender_display = presc_request.pharmacy.pharmacy_name or m.sender.username

            data.append({
                'id': m.id,
                'message_text': m.message_text,
                'sender_id': m.sender_id,
                'sender_name': sender_display,
                'is_current_user': (m.sender_id == request.user.id),
                'created_at': m.created_at.isoformat(),
                'is_read': m.is_read,
                'read_at': m.read_at.isoformat() if m.read_at else None,
            })

        return JsonResponse({
            'status': 'success',
            'request_id': presc_request.id,
            'request_status': presc_request.status,
            'is_writable': (presc_request.status in PrescriptionRequest.ACTIVE_STATUSES),
            'count': len(data),
            'messages': data,
        }, status=200)

    # =========================================================================
    # POST: Send Message
    # =========================================================================
    if request.method == 'POST':
        # Enforce writable status check
        if presc_request.status not in PrescriptionRequest.ACTIVE_STATUSES:
            return JsonResponse({
                'error': f"Messaging is archived. Requests in {presc_request.get_status_display()} status are read-only."
            }, status=403)

        # Extract message content from JSON or form-data
        if request.content_type == 'application/json':
            try:
                payload = json.loads(request.body.decode('utf-8'))
                message_text = payload.get('message_text', '')
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JsonResponse({'error': 'Invalid JSON format in payload.'}, status=400)
        else:
            message_text = request.POST.get('message_text', '')

        # Content validation
        if not isinstance(message_text, str) or not message_text.strip():
            return JsonResponse({'error': 'Message content cannot be empty or whitespace-only.'}, status=400)

        if len(message_text) > 1000:
            return JsonResponse({'error': 'Message content cannot exceed 1000 characters.'}, status=400)

        # Atomic persistence of message and NEW_MESSAGE notification
        with transaction.atomic():
            msg = RequestMessage(
                request=presc_request,
                sender=sender,
                recipient=recipient,
                message_text=message_text,
            )
            msg.full_clean()
            msg.save()

            # Create in-app notification for the recipient
            if is_patient:
                sender_display = patient_user.get_full_name() or patient_user.username
            else:
                sender_display = presc_request.pharmacy.pharmacy_name

            create_request_notification(
                request=presc_request,
                recipient=recipient,
                notification_type='NEW_MESSAGE',
                title='New Message',
                message=f"New message from {sender_display} regarding request #{presc_request.id}.",
                actor=sender
            )

        return JsonResponse({
            'status': 'success',
            'message': {
                'id': msg.id,
                'message_text': msg.message_text,
                'sender_id': msg.sender_id,
                'sender_name': sender_display,
                'is_current_user': True,
                'created_at': msg.created_at.isoformat(),
                'is_read': msg.is_read,
                'read_at': None,
            }
        }, status=201)

