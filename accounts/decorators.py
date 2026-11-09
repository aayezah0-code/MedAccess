from functools import wraps
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages


def get_predefined_admin_emails():
    """
    Returns normalized (lowercase, stripped) set of predefined admin emails from settings.
    """
    raw_emails = getattr(settings, 'PREDEFINED_ADMIN_EMAILS', [
        'aayezahali786@gmail.com',
        'abhimanyu1sandeep@gmail.com',
    ])
    if isinstance(raw_emails, str):
        raw_emails = raw_emails.split(',')
    return {email.strip().lower() for email in raw_emails if email.strip()}


def is_predefined_admin(user):
    """
    Verifies that the user:
    - Is authenticated and active.
    - Has role == 'ADMIN'.
    - Has is_staff == True and is_superuser == True.
    - Has an email that matches one of the predefined admin email identities (case-insensitive).
    """
    if not user or not user.is_authenticated:
        return False
    if not user.is_active:
        return False
    if getattr(user, 'role', None) != 'ADMIN':
        return False
    if not getattr(user, 'is_staff', False) or not getattr(user, 'is_superuser', False):
        return False
    email = (getattr(user, 'email', '') or '').strip().lower()
    if not email:
        return False
    return email in get_predefined_admin_emails()


def predefined_admin_required(view_func):
    """
    Strict authorization decorator for MedAccess Admin Portal views.
    - Unauthenticated requests are redirected to accounts:login with safe ?next= param.
    - Authenticated but unauthorized requests (wrong role, unlisted admin/staff/superuser,
      patient, pharmacy, inactive admin) receive HTTP 403 Forbidden via PermissionDenied.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Please log in to access the Admin Portal.")
            login_url = reverse('accounts:login')
            return redirect(f"{login_url}?next={request.get_full_path()}")

        if not is_predefined_admin(request.user):
            raise PermissionDenied("Access denied. You are not an authorized administrator.")

        return view_func(request, *args, **kwargs)
    return _wrapped_view


def role_required(allowed_roles):
    """
    Decorator to restrict view access to users with specified role(s).
    Usage:
        @role_required('ADMIN')
        @role_required(['PATIENT', 'PHARMACY'])
    """
    if isinstance(allowed_roles, str):
        allowed_roles = [allowed_roles]

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                messages.error(request, "Please log in to access this page.")
                return redirect('accounts:login')

            if request.user.role not in allowed_roles:
                messages.error(request, "Access denied. You do not have permission for this portal.")
                return redirect('landing')

            if request.user.role == 'ADMIN':
                if not is_predefined_admin(request.user):
                    raise PermissionDenied("Access denied. You are not an authorized administrator.")

            if request.user.role == 'PHARMACY':
                profile = getattr(request.user, 'pharmacy_profile', None)
                if not profile or profile.approval_status != 'APPROVED':
                    messages.warning(
                        request,
                        "Your pharmacy account is not approved yet. Access is restricted until admin verification."
                    )
                    return redirect('landing')

            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


