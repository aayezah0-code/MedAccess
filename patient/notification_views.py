"""
Shared Participant-Aware Notification Center Views for MedAccess.

Provides backend API endpoints for retrieving notifications, checking unread counts,
and marking notifications as read for authenticated patients and approved pharmacies.
"""

import logging

from django.core.paginator import Paginator
from django.http import JsonResponse, Http404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from patient.models import Notification

logger = logging.getLogger(__name__)


def check_notification_user_auth(request):
    """
    Validates that user is authenticated and, if role == 'PHARMACY', that
    pharmacy profile is approved.
    Returns (authorized: bool, error_response: JsonResponse | None).
    """
    if not request.user.is_authenticated:
        return False, JsonResponse({'error': 'Authentication required.'}, status=401)

    if getattr(request.user, 'role', '') == 'PHARMACY':
        pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
        if not pharmacy_profile or pharmacy_profile.approval_status != 'APPROVED':
            return False, JsonResponse({'error': 'Pharmacy account is not approved.'}, status=403)

    return True, None


def resolve_notification_navigation_url(user, prescription_request_id):
    """
    Generates internal navigation URL server-side based on user's role:
    - Patient recipient -> corresponding patient request detail page
    - Pharmacy recipient -> corresponding pharmacy request detail page
    """
    if not prescription_request_id:
        return ''

    role = getattr(user, 'role', '')
    if role == 'PHARMACY':
        return reverse('pharmacy:request_detail', kwargs={'pk': prescription_request_id})
    return reverse('patient:request_detail', kwargs={'pk': prescription_request_id})


@require_GET
def notifications_list_api(request):
    """
    GET /patient/notifications/api/
    GET /pharmacy/notifications/api/

    Retrieves paginated notifications strictly scoped to recipient=request.user.
    Newest first, default page size of 20.
    Returns notification fields, role-based navigation URL, total unread count,
    and pagination metadata.
    """
    authorized, error_resp = check_notification_user_auth(request)
    if not authorized:
        return error_resp

    qs = Notification.objects.filter(recipient=request.user).order_by('-created_at')

    # Optional filter parameter: filter=unread
    filter_mode = request.GET.get('filter', '').strip().lower()
    if filter_mode == 'unread':
        qs = qs.filter(is_read=False)

    # Precompute user's total unread notifications count
    total_unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    # Pagination: 20 items per page
    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    data = []
    for notif in page_obj:
        data.append({
            'id': notif.id,
            'notification_type': notif.notification_type,
            'notification_type_display': notif.get_notification_type_display(),
            'title': notif.title,
            'message': notif.message,
            'is_read': notif.is_read,
            'read_at': notif.read_at.isoformat() if notif.read_at else None,
            'created_at': notif.created_at.isoformat(),
            'prescription_request_id': notif.prescription_request_id,
            'navigation_url': resolve_notification_navigation_url(request.user, notif.prescription_request_id),
        })

    return JsonResponse({
        'status': 'success',
        'notifications': data,
        'unread_count': total_unread_count,
        'pagination': {
            'page': page_obj.number,
            'num_pages': paginator.num_pages,
            'total_count': paginator.count,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        }
    }, status=200)


@require_GET
def notification_unread_count_api(request):
    """
    GET /patient/notifications/unread-count/
    GET /pharmacy/notifications/unread-count/

    Returns the count of unread notifications for the authenticated user without
    retrieving full records.
    """
    authorized, error_resp = check_notification_user_auth(request)
    if not authorized:
        return error_resp

    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return JsonResponse({
        'status': 'success',
        'unread_count': unread_count,
    }, status=200)


@require_POST
def notification_mark_read_api(request, pk):
    """
    POST /patient/notifications/<pk>/read/
    POST /pharmacy/notifications/<pk>/read/

    Marks a single notification belonging to recipient=request.user as read.
    Preserves original read_at if already marked read.
    Returns 404 if notification belongs to another user or does not exist.
    """
    authorized, error_resp = check_notification_user_auth(request)
    if not authorized:
        return error_resp

    try:
        notif = Notification.objects.get(pk=pk, recipient=request.user)
    except Notification.DoesNotExist:
        raise Http404("Notification not found.")

    was_unread = not notif.is_read
    if was_unread:
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=['is_read', 'read_at'])

    return JsonResponse({
        'status': 'success',
        'notification_id': notif.id,
        'is_read': True,
        'read_at': notif.read_at.isoformat() if notif.read_at else None,
        'was_unread': was_unread,
    }, status=200)


@require_POST
def notifications_mark_all_read_api(request):
    """
    POST /patient/notifications/mark-all-read/
    POST /pharmacy/notifications/mark-all-read/

    Marks all unread notifications for recipient=request.user as read in a single
    bulk update. Returns the count of updated records.
    """
    authorized, error_resp = check_notification_user_auth(request)
    if not authorized:
        return error_resp

    now = timezone.now()
    updated_count = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).update(is_read=True, read_at=now)

    return JsonResponse({
        'status': 'success',
        'updated_count': updated_count,
    }, status=200)

