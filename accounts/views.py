import time
import logging
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.views import (
    PasswordResetView,
    PasswordResetDoneView,
    PasswordResetConfirmView,
    PasswordResetCompleteView,
)
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe

from accounts.models import User
from accounts.decorators import is_predefined_admin
from .forms import (
    LoginForm,
    PatientRegistrationForm,
    PharmacyRegistrationForm,
    MedAccessPasswordResetForm,
    MedAccessSetPasswordForm,
    ResendVerificationForm,
)
from .utils import (
    send_verification_email,
    validate_verification_token,
)
logger = logging.getLogger(__name__)


def landing_view(request):
    """
    Public landing page for MedAccess.
    """
    return render(request, 'landing/index.html')


def register_patient_view(request):
    """
    Patient self-registration view.
    Creates an unverified account and dispatches a time-limited email verification link.
    """
    if request.user.is_authenticated:
        return redirect('landing')

    if request.method == 'POST':
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            email_sent = send_verification_email(request, user)
            if email_sent:
                messages.success(
                    request,
                    "Your account has been created successfully. "
                    "We have sent a verification link to your registered email address. "
                    "Please verify your email before logging in."
                )
            else:
                messages.warning(
                    request,
                    "Your account has been created, but we could not deliver the verification email right now. "
                    "Please use the resend verification option to receive your link."
                )
            return render(
                request,
                'accounts/verify_email_status.html',
                {
                    'status': 'pending',
                    'email': user.email,
                    'email_sent': email_sent,
                    'target_user': user,
                }
            )
    else:
        form = PatientRegistrationForm()

    return render(request, 'accounts/register_patient.html', {'form': form})



def register_pharmacy_view(request):
    """
    Pharmacy self-registration.
    Creates an unverified account with pending admin approval
    and displays the email verification status page.
    """
    if request.user.is_authenticated:
        return redirect('landing')

    if request.method == 'POST':
        form = PharmacyRegistrationForm(request.POST)

        if form.is_valid():
            user = form.save()
            email_sent = send_verification_email(request, user)

            if email_sent:
                messages.success(
                    request,
                    "Your pharmacy account has been created successfully. "
                    "Please verify your email before logging in."
                )
            else:
                messages.warning(
                    request,
                    "Your pharmacy account has been created, but we could not "
                    "send the verification email. Please request a new link."
                )

            return render(
                request,
                'accounts/verify_email_status.html',
                {
                    'status': 'pending',
                    'email': user.email,
                    'email_sent': email_sent,
                    'target_user': user,
                }
            )
    else:
        form = PharmacyRegistrationForm()

    return render(
        request,
        'accounts/register_pharmacy.html',
        {'form': form}
    )



def login_view(request):
    """
    Unified login view for Patient, Pharmacy, and Admin.
    Enforces email verification for Patient and Pharmacy accounts while
    preserving existing Admin access and Pharmacy approval restrictions.
    """
    next_url = request.POST.get('next') or request.GET.get('next') or ''

    if request.user.is_authenticated:
        if is_predefined_admin(request.user):
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect('admin_portal:landing')
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect('landing')

    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']

            user = authenticate(request, username=username, password=password)

            if user is None:
                messages.error(request, "Invalid username or password. Please try again.")
            else:
                # 1. Admin accounts bypass email verification and approval restrictions
                if user.role != 'ADMIN' and not user.is_superuser and not user.is_staff:
                    # 2. Email verification check for Patient and Pharmacy accounts
                    if not user.email_verified:
                        resend_url = reverse('accounts:resend_verification')
                        if user.role == 'PATIENT':
                            messages.error(
                                request,
                                mark_safe(
                                    "Please verify your email address before logging in. "
                                    f'<a href="{resend_url}" style="color: inherit; text-decoration: underline; font-weight: 600;">Resend verification email</a>'
                                )
                            )
                        else:  # PHARMACY
                            messages.error(
                                request,
                                mark_safe(
                                    "Please verify your pharmacy official email address before logging in. "
                                    f'<a href="{resend_url}" style="color: inherit; text-decoration: underline; font-weight: 600;">Resend verification email</a>'
                                )
                            )
                        return render(request, 'accounts/login.html', {'form': form, 'next': next_url})

                # 3. Pharmacy approval check (evaluated for verified pharmacy users)
                if user.role == 'PHARMACY':
                    profile = getattr(user, 'pharmacy_profile', None)
                    if profile and profile.approval_status == 'PENDING':
                        messages.warning(
                            request,
                            "Your pharmacy registration is currently pending admin approval. "
                            "Access will be granted once your license details are verified."
                        )
                        return render(request, 'accounts/login.html', {'form': form, 'next': next_url})
                    elif profile and profile.approval_status == 'REJECTED':
                        if next_url and 'resubmit' in next_url:
                            login(request, user)
                            messages.info(
                                request,
                                "Your application was rejected. Please review administrator feedback and update your details below."
                            )
                            return redirect('pharmacy:resubmit')
                        resubmit_url = reverse('pharmacy:resubmit')
                        messages.error(
                            request,
                            mark_safe(
                                "Your pharmacy registration application was reviewed and rejected. "
                                f'<a href="{resubmit_url}" style="color: inherit; text-decoration: underline; font-weight: 600;">Update registration details &amp; resubmit</a>'
                            )
                        )
                        return render(request, 'accounts/login.html', {'form': form, 'next': next_url})

                # 4. Check administrative account authorization
                # Unlisted ADMIN, staff, or superusers must be denied administrative access
                if user.role == 'ADMIN' or user.is_staff or user.is_superuser:
                    if not is_predefined_admin(user):
                        login(request, user)
                        messages.error(request, "Access denied. Administrative access is restricted to authorized identities.")
                        return redirect('landing')

                # 5. Authentication successful
                login(request, user)
                messages.success(request, f"Welcome back, {user.username}!")

                # 6. Post-login redirect routing
                is_safe_next = next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()})

                if is_predefined_admin(user):
                    if is_safe_next:
                        return redirect(next_url)
                    return redirect('admin_portal:landing')

                # Non-admin user routing (Patient / Pharmacy)
                if is_safe_next:
                    clean_next = next_url.strip()
                    if clean_next.startswith('/admin/') or clean_next.startswith('/admin-portal/'):
                        messages.warning(request, "Access restricted.")
                        return redirect('landing')
                    return redirect(next_url)

                return redirect('landing')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form, 'next': next_url})


@require_POST
def logout_view(request):
    """
    Secure POST logout view.
    """
    if request.user.is_authenticated:
        logout(request)
        messages.info(request, "You have been logged out successfully.")
    return redirect('landing')



def verify_email_view(request, token=None):
    """
    Verifies a user's email using the signed verification token.
    Safe from unhandled 500 exceptions; logs full traceback securely.
    """
    try:
        raw_token = (token or request.GET.get('token') or '').strip()

        user, token_status = validate_verification_token(raw_token)
        view_status = token_status

        if token_status == 'valid' and user:
            user.email_verified = True
            user.save(update_fields=['email_verified', 'updated_at'])
            view_status = 'success'
            logger.info("User pk=%s email verified successfully", user.pk)

        context = {
            'status': view_status,
            'target_user': user,
            'user': None,  # Prevent shadowing request.user in base.html
        }

        return render(
            request,
            'accounts/verify_email_status.html',
            context
        )

    except Exception as exc:
        logger.exception(
            "Unexpected error in verify_email_view: %s", type(exc).__name__
        )
        return render(
            request,
            'accounts/verify_email_status.html',
            {
                'status': 'invalid',
                'target_user': None,
                'user': None,
            }
        )




def resend_verification_view(request):
    """
    Allows unverified users to request a fresh verification link.
    Safe against unhandled 500 crashes; enforces 60s session rate limiting.
    """
    try:
        if request.method == 'POST':
            form = ResendVerificationForm(request.POST)

            if form.is_valid():
                current_time = time.time()
                last_time = request.session.get(
                    'resend_verification_last_time', 0
                )

                # Limit requests to one per 60 seconds per session
                if current_time - last_time < 60:
                    messages.warning(
                        request,
                        "A verification email was requested recently. "
                        "Please check your inbox or wait a moment before requesting another."
                    )
                    return render(
                        request,
                        'accounts/resend_verification.html',
                        {'form': form}
                    )

                email = form.cleaned_data['email'].strip().lower()

                request.session['resend_verification_last_time'] = (
                    current_time
                )
                request.session.modified = True

                user = User.objects.filter(
                    email__iexact=email
                ).first()

                if user and not user.email_verified:
                    email_sent = send_verification_email(request, user)
                    if email_sent:
                        messages.success(
                            request,
                            "If an unverified account with this email exists, "
                            "a new verification link has been sent. "
                            "Please check your inbox."
                        )
                    else:
                        messages.warning(
                            request,
                            "Your account was found, but we could not deliver the email "
                            "at this moment due to a mail server connection issue. "
                            "Please try again shortly or contact support."
                        )
                elif user and user.email_verified:
                    messages.info(
                        request,
                        "This email address is already verified. You can log in directly."
                    )
                else:
                    messages.success(
                        request,
                        "If an unverified account with this email exists, "
                        "a new verification link has been sent. "
                        "Please check your inbox."
                    )

                return redirect('accounts:login')

        else:
            form = ResendVerificationForm()

        return render(
            request,
            'accounts/resend_verification.html',
            {'form': form}
        )

    except Exception as exc:
        logger.exception(
            "Unexpected error in resend_verification_view: %s", type(exc).__name__
        )
        messages.error(
            request,
            "An unexpected error occurred while processing your request. Please try again."
        )
        return render(
            request,
            'accounts/resend_verification.html',
            {'form': ResendVerificationForm()}
        )



class MedAccessPasswordResetView(PasswordResetView):
    """
    Initiates password reset process for registered patient and pharmacy users.
    Sends time-limited reset link to registered email and displays generic success response.
    """
    template_name = 'accounts/password_reset_form.html'
    email_template_name = 'accounts/password_reset_email.html'
    subject_template_name = 'accounts/password_reset_subject.txt'
    form_class = MedAccessPasswordResetForm
    success_url = reverse_lazy('accounts:password_reset_done')


class MedAccessPasswordResetDoneView(PasswordResetDoneView):
    """
    Displays generic confirmation page indicating a reset link was sent if the email exists.
    """
    template_name = 'accounts/password_reset_done.html'


class MedAccessPasswordResetConfirmView(PasswordResetConfirmView):
    """
    Validates password reset token, displays set new password form, and enforces
    all Django password validation rules.
    """
    template_name = 'accounts/password_reset_confirm.html'
    form_class = MedAccessSetPasswordForm
    success_url = reverse_lazy('accounts:password_reset_complete')


class MedAccessPasswordResetCompleteView(PasswordResetCompleteView):
    """
    Displays success confirmation that password was updated and provides direct login link.
    """
    template_name = 'accounts/password_reset_complete.html'

