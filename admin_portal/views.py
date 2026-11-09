from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Avg, Sum, F
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import predefined_admin_required
from accounts.models import User
from patient.models import Prescription, PrescriptionRequest, PatientProfile
from pharmacy.models import PharmacyProfile, Medicine, Inventory, Feedback
from admin_portal.models import PlatformSetting
from admin_portal.forms import (
    AdminPatientCreateForm,
    AdminPatientEditForm,
    PharmacyRejectForm,
    PlatformSettingsForm,
)
from admin_portal.services.email_service import (
    send_pharmacy_approval_email,
    send_pharmacy_rejection_email,
)


# ==============================================================================
# 1. ADMIN DASHBOARD
# ==============================================================================

@predefined_admin_required
def dashboard_view(request):
    """
    Main MedAccess Admin Portal Dashboard view.
    Gathers operational platform metrics and statistics using efficient ORM aggregate queries.
    Protected strictly by @predefined_admin_required.
    """
    total_patients = User.objects.filter(role='PATIENT').count()

    pharmacy_stats = PharmacyProfile.objects.aggregate(
        total=Count('id'),
        approved=Count('id', filter=Q(approval_status='APPROVED')),
        pending=Count('id', filter=Q(approval_status='PENDING')),
        rejected=Count('id', filter=Q(approval_status='REJECTED')),
    )

    total_prescriptions = Prescription.objects.count()

    request_stats = PrescriptionRequest.objects.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='COMPLETED')),
        pending=Count('id', filter=Q(status='PENDING')),
        accepted=Count('id', filter=Q(status='ACCEPTED')),
        processing=Count('id', filter=Q(status='PROCESSING')),
        ready_for_pickup=Count('id', filter=Q(status='READY_FOR_PICKUP')),
        rejected=Count('id', filter=Q(status='REJECTED')),
        cancelled=Count('id', filter=Q(status='CANCELLED')),
    )

    recent_requests = (
        PrescriptionRequest.objects
        .select_related('patient__user', 'pharmacy')
        .order_by('-created_at')[:5]
    )

    recent_pharmacies = (
        PharmacyProfile.objects
        .select_related('user')
        .order_by('-created_at')[:5]
    )

    context = {
        'admin_user': request.user,
        'stats': {
            'total_patients': total_patients,
            'total_pharmacies': pharmacy_stats['total'] or 0,
            'approved_pharmacies': pharmacy_stats['approved'] or 0,
            'pending_pharmacies': pharmacy_stats['pending'] or 0,
            'rejected_pharmacies': pharmacy_stats['rejected'] or 0,
            'total_prescriptions': total_prescriptions,
            'total_requests': request_stats['total'] or 0,
            'completed_requests': request_stats['completed'] or 0,
            'pending_requests': request_stats['pending'] or 0,
            'accepted_requests': request_stats['accepted'] or 0,
            'processing_requests': request_stats['processing'] or 0,
            'ready_requests': request_stats['ready_for_pickup'] or 0,
            'rejected_requests': request_stats['rejected'] or 0,
            'cancelled_requests': request_stats['cancelled'] or 0,
        },
        'recent_requests': recent_requests,
        'recent_pharmacies': recent_pharmacies,
    }

    return render(request, 'admin_portal/dashboard.html', context)


landing_view = dashboard_view


# ==============================================================================
# 2. PATIENT MANAGEMENT
# ==============================================================================

@predefined_admin_required
def patient_list_view(request):
    """
    List, search, filter, and paginate registered patients.
    """
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', 'all').strip().lower()

    base_qs = (
        User.objects.filter(role='PATIENT')
        .select_related('patient_profile')
        .annotate(
            prescriptions_count=Count('patient_profile__prescriptions', distinct=True),
            requests_count=Count('patient_profile__prescription_requests', distinct=True)
        )
        .order_by('-date_joined')
    )

    total_patients_count = base_qs.count()
    active_patients_count = base_qs.filter(is_active=True).count()
    inactive_patients_count = total_patients_count - active_patients_count

    if search_query:
        base_qs = base_qs.filter(
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(phone_number__icontains=search_query)
        )

    if status_filter == 'active':
        base_qs = base_qs.filter(is_active=True)
    elif status_filter == 'inactive':
        base_qs = base_qs.filter(is_active=False)

    paginator = Paginator(base_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'patients': page_obj,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': total_patients_count,
        'active_count': active_patients_count,
        'inactive_count': inactive_patients_count,
        'filtered_count': paginator.count,
    }
    return render(request, 'admin_portal/patients_list.html', context)


@predefined_admin_required
def patient_detail_view(request, pk):
    """
    Detailed audit view for a single patient profile with activity history.
    """
    patient_user = get_object_or_404(User.objects.select_related('patient_profile'), pk=pk, role='PATIENT')
    profile = getattr(patient_user, 'patient_profile', None)

    recent_prescriptions = []
    recent_requests = []
    stats = {'total_rx': 0, 'total_requests': 0, 'completed_requests': 0}

    if profile:
        recent_prescriptions = profile.prescriptions.order_by('-uploaded_at')[:5]
        recent_requests = (
            profile.prescription_requests
            .select_related('pharmacy')
            .order_by('-created_at')[:5]
        )
        stats['total_rx'] = profile.prescriptions.count()
        stats['total_requests'] = profile.prescription_requests.count()
        stats['completed_requests'] = profile.prescription_requests.filter(status='COMPLETED').count()

    context = {
        'patient_user': patient_user,
        'profile': profile,
        'recent_prescriptions': recent_prescriptions,
        'recent_requests': recent_requests,
        'stats': stats,
    }
    return render(request, 'admin_portal/patient_detail.html', context)


@predefined_admin_required
def patient_add_view(request):
    """
    Provision a new patient account from the Admin Portal.
    """
    if request.method == 'POST':
        form = AdminPatientCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"Patient account '{user.username}' created successfully.")
            return redirect('admin_portal:patient_detail', pk=user.pk)
    else:
        form = AdminPatientCreateForm()

    return render(request, 'admin_portal/patient_form.html', {
        'form': form,
        'is_edit': False,
    })


@predefined_admin_required
def patient_edit_view(request, pk):
    """
    Edit existing patient personal and clinical profile information.
    """
    patient_user = get_object_or_404(User.objects.select_related('patient_profile'), pk=pk, role='PATIENT')

    if request.method == 'POST':
        form = AdminPatientEditForm(request.POST, patient_user=patient_user)
        if form.is_valid():
            form.save()
            messages.success(request, f"Patient account '{patient_user.username}' updated successfully.")
            return redirect('admin_portal:patient_detail', pk=patient_user.pk)
    else:
        form = AdminPatientEditForm(patient_user=patient_user)

    return render(request, 'admin_portal/patient_form.html', {
        'form': form,
        'patient_user': patient_user,
        'is_edit': True,
    })


@predefined_admin_required
@require_POST
def patient_toggle_status_view(request, pk):
    """
    Toggle active/inactive status or perform safe audit deletion of a patient account.
    """
    patient_user = get_object_or_404(User, pk=pk, role='PATIENT')
    action = request.POST.get('action', 'toggle').strip().lower()

    profile = getattr(patient_user, 'patient_profile', None)
    has_medical_records = False
    if profile:
        has_medical_records = (
            profile.prescriptions.exists() or
            profile.prescription_requests.exists()
        )

    if action == 'delete':
        if has_medical_records:
            patient_user.is_active = False
            patient_user.save(update_fields=['is_active'])
            messages.warning(
                request,
                f"Patient '{patient_user.username}' has linked medical prescriptions/requests. "
                "To preserve clinical audit history, the account has been deactivated instead of permanently deleted."
            )
        else:
            username = patient_user.username
            patient_user.delete()
            messages.success(request, f"Patient account '{username}' was permanently deleted.")
    else:
        # Toggle is_active
        patient_user.is_active = not patient_user.is_active
        patient_user.save(update_fields=['is_active'])
        status_label = "activated" if patient_user.is_active else "deactivated"
        messages.success(request, f"Patient account '{patient_user.username}' has been {status_label}.")

    return redirect('admin_portal:patient_list')


# ==============================================================================
# 3. PHARMACY MANAGEMENT & VERIFICATION
# ==============================================================================

@predefined_admin_required
def pharmacy_list_view(request):
    """
    Comprehensive directory of pharmacies with status filtering, search, and metrics.
    """
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', 'all').strip().upper()

    base_qs = (
        PharmacyProfile.objects
        .select_related('user')
        .annotate(
            total_inventory_batches=Count('inventory', distinct=True),
            total_fulfilled_requests=Count(
                'received_prescription_requests',
                filter=Q(received_prescription_requests__status='COMPLETED'),
                distinct=True
            )
        )
        .order_by('-created_at')
    )

    counts = PharmacyProfile.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(approval_status='PENDING')),
        approved=Count('id', filter=Q(approval_status='APPROVED')),
        rejected=Count('id', filter=Q(approval_status='REJECTED')),
    )

    if search_query:
        base_qs = base_qs.filter(
            Q(pharmacy_name__icontains=search_query) |
            Q(owner_name__icontains=search_query) |
            Q(pharmacist_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(pharmacy_license_number__icontains=search_query) |
            Q(gst_number__icontains=search_query) |
            Q(user__username__icontains=search_query)
        )

    if status_filter in ['PENDING', 'APPROVED', 'REJECTED']:
        base_qs = base_qs.filter(approval_status=status_filter)

    paginator = Paginator(base_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'pharmacies': page_obj,
        'search_query': search_query,
        'status_filter': status_filter,
        'counts': counts,
        'filtered_count': paginator.count,
    }
    return render(request, 'admin_portal/pharmacy_list.html', context)


@predefined_admin_required
def pharmacy_detail_view(request, pk):
    """
    Verification dossier for a single pharmacy with approval/rejection actions.
    """
    pharmacy = get_object_or_404(PharmacyProfile.objects.select_related('user'), pk=pk)

    recent_requests = (
        pharmacy.received_prescription_requests
        .select_related('patient__user')
        .order_by('-created_at')[:5]
    )

    inventory_count = pharmacy.inventory.count()
    completed_requests_count = pharmacy.received_prescription_requests.filter(status='COMPLETED').count()

    reject_form = PharmacyRejectForm(initial={'rejection_reason': pharmacy.rejection_reason or ''})

    context = {
        'pharmacy': pharmacy,
        'recent_requests': recent_requests,
        'inventory_count': inventory_count,
        'completed_requests_count': completed_requests_count,
        'reject_form': reject_form,
    }
    return render(request, 'admin_portal/pharmacy_detail.html', context)


@predefined_admin_required
@require_POST
def pharmacy_approve_view(request, pk):
    """
    Approve pharmacy credentials with atomic lock, grant access, send approval email.
    """
    with transaction.atomic():
        pharmacy = get_object_or_404(
            PharmacyProfile.objects.select_for_update().select_related('user'),
            pk=pk
        )
        pharmacy.approval_status = 'APPROVED'
        pharmacy.rejection_reason = None
        pharmacy.save(update_fields=['approval_status', 'rejection_reason'])

        if pharmacy.user:
            pharmacy.user.is_approved = True
            pharmacy.user.save(update_fields=['is_approved'])

        # Send official email notification
        send_pharmacy_approval_email(pharmacy)

    messages.success(request, f"Pharmacy '{pharmacy.pharmacy_name}' has been successfully approved.")
    return redirect('admin_portal:pharmacy_detail', pk=pk)


@predefined_admin_required
@require_POST
def pharmacy_reject_view(request, pk):
    """
    Reject pharmacy application with optional reason, restrict access, dispatch email.
    """
    reject_form = PharmacyRejectForm(request.POST)
    rejection_reason = ''
    if reject_form.is_valid():
        rejection_reason = (reject_form.cleaned_data.get('rejection_reason') or '').strip()

    with transaction.atomic():
        pharmacy = get_object_or_404(
            PharmacyProfile.objects.select_for_update().select_related('user'),
            pk=pk
        )
        pharmacy.approval_status = 'REJECTED'
        pharmacy.rejection_reason = rejection_reason or None
        pharmacy.save(update_fields=['approval_status', 'rejection_reason'])

        if pharmacy.user:
            pharmacy.user.is_approved = False
            pharmacy.user.save(update_fields=['is_approved'])

        # Send official email notification with optional reason
        send_pharmacy_rejection_email(pharmacy, rejection_reason=pharmacy.rejection_reason)

    messages.warning(request, f"Pharmacy '{pharmacy.pharmacy_name}' has been rejected.")
    return redirect('admin_portal:pharmacy_detail', pk=pk)


# ==============================================================================
# 4. MEDICINES & INVENTORY MANAGEMENT
# ==============================================================================

@predefined_admin_required
def medicine_inventory_view(request):
    """
    Unified medicine catalogue & pharmacy-wise inventory overview.
    Accurately separates stock quantities by unit to prevent invalid mathematical additions.
    """
    today = timezone.localdate()
    view_mode = request.GET.get('tab', 'catalogue').strip().lower()
    search_query = request.GET.get('q', '').strip()
    filter_type = request.GET.get('filter', 'all').strip().lower()
    pharmacy_id = request.GET.get('pharmacy', '').strip()

    # Core Metrics
    total_medicines_count = Medicine.objects.count()
    total_batches_count = Inventory.objects.count()
    low_stock_count = Inventory.objects.filter(quantity__lte=F('low_stock_limit'), quantity__gt=0).count()
    out_of_stock_count = Inventory.objects.filter(quantity=0).count()
    expired_count = Inventory.objects.filter(expiry_date__lt=today).count()

    # Accurate stock breakdown by unit
    unit_stock_summary = (
        Inventory.objects
        .values('unit')
        .annotate(total_qty=Sum('quantity'), batch_count=Count('id'))
        .order_by('-total_qty')
    )

    # All approved pharmacies for filter dropdown
    pharmacies_list = PharmacyProfile.objects.filter(approval_status='APPROVED').order_by('pharmacy_name')

    if view_mode == 'inventory':
        inv_qs = Inventory.objects.select_related('medicine', 'pharmacy').order_by('-updated_at')

        if search_query:
            inv_qs = inv_qs.filter(
                Q(medicine__medicine_name__icontains=search_query) |
                Q(medicine__manufacturer__icontains=search_query) |
                Q(batch_number__icontains=search_query) |
                Q(pharmacy__pharmacy_name__icontains=search_query)
            )

        if pharmacy_id.isdigit():
            inv_qs = inv_qs.filter(pharmacy_id=int(pharmacy_id))

        if filter_type == 'low_stock':
            inv_qs = inv_qs.filter(quantity__lte=F('low_stock_limit'), quantity__gt=0)
        elif filter_type == 'out_of_stock':
            inv_qs = inv_qs.filter(quantity=0)
        elif filter_type == 'expired':
            inv_qs = inv_qs.filter(expiry_date__lt=today)

        paginator = Paginator(inv_qs, 20)
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)
    else:
        med_qs = (
            Medicine.objects
            .annotate(
                total_batches=Count('inventory_records', distinct=True),
                total_stock_count=Sum('inventory_records__quantity')
            )
            .order_by('medicine_name')
        )

        if search_query:
            med_qs = med_qs.filter(
                Q(medicine_name__icontains=search_query) |
                Q(manufacturer__icontains=search_query) |
                Q(composition__icontains=search_query) |
                Q(dosage_form__icontains=search_query)
            )

        paginator = Paginator(med_qs, 20)
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)

    context = {
        'view_mode': view_mode,
        'items': page_obj,
        'search_query': search_query,
        'filter_type': filter_type,
        'selected_pharmacy': pharmacy_id,
        'pharmacies_list': pharmacies_list,
        'metrics': {
            'total_medicines': total_medicines_count,
            'total_batches': total_batches_count,
            'low_stock': low_stock_count,
            'out_of_stock': out_of_stock_count,
            'expired': expired_count,
        },
        'unit_stock_summary': unit_stock_summary,
        'today': today,
    }
    return render(request, 'admin_portal/medicine_inventory.html', context)


# ==============================================================================
# 5. FEEDBACK & ISSUES
# ==============================================================================

@predefined_admin_required
def feedback_list_view(request):
    """
    Patient reviews and pharmacy ratings oversight.
    """
    search_query = request.GET.get('q', '').strip()
    rating_filter = request.GET.get('rating', 'all').strip()

    base_qs = (
        Feedback.objects
        .select_related('patient__user', 'pharmacy')
        .order_by('-created_at')
    )

    total_feedback = base_qs.count()
    avg_rating_val = base_qs.aggregate(avg=Avg('rating'))['avg'] or 0.0

    # Rating distribution 1 to 5
    rating_distribution = []
    for star in [5, 4, 3, 2, 1]:
        cnt = base_qs.filter(rating=star).count()
        pct = (cnt / total_feedback * 100) if total_feedback > 0 else 0
        rating_distribution.append({
            'star': star,
            'count': cnt,
            'percentage': round(pct, 1),
        })

    if search_query:
        base_qs = base_qs.filter(
            Q(comment__icontains=search_query) |
            Q(pharmacy__pharmacy_name__icontains=search_query) |
            Q(patient__user__username__icontains=search_query) |
            Q(patient__user__first_name__icontains=search_query)
        )

    if rating_filter in ['1', '2', '3', '4', '5']:
        base_qs = base_qs.filter(rating=int(rating_filter))

    paginator = Paginator(base_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'reviews': page_obj,
        'search_query': search_query,
        'rating_filter': rating_filter,
        'total_feedback': total_feedback,
        'avg_rating': round(avg_rating_val, 1),
        'rating_distribution': rating_distribution,
        'filtered_count': paginator.count,
    }
    return render(request, 'admin_portal/feedback_list.html', context)


# ==============================================================================
# 6. PLATFORM SETTINGS
# ==============================================================================

@predefined_admin_required
def platform_settings_view(request):
    """
    Platform operational settings management. Strictly non-sensitive configuration.
    """
    settings_obj = PlatformSetting.get_settings()

    if request.method == 'POST':
        form = PlatformSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            cfg = form.save(commit=False)
            cfg.updated_by = request.user
            cfg.save()
            messages.success(request, "Platform operational configuration saved successfully.")
            return redirect('admin_portal:settings')
    else:
        form = PlatformSettingsForm(instance=settings_obj)

    return render(request, 'admin_portal/settings.html', {
        'form': form,
        'settings_obj': settings_obj,
    })


# ==============================================================================
# 7. HELP & SUPPORT
# ==============================================================================

@predefined_admin_required
def help_support_view(request):
    """
    Admin user guide, platform operational workflows, support FAQs, and escalation channels.
    """
    return render(request, 'admin_portal/help.html', {
        'admin_user': request.user,
    })
