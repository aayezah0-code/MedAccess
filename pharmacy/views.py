import json
import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q, Avg, Count, Sum
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from patient.models import PrescriptionRequest, PrescriptionRequestItem
from patient.services.notification_service import create_request_notification
from pharmacy.models import Inventory, Medicine, Feedback, MedicineComposition, PharmacySupportRequest
from pharmacy.services.composition_lookup import lookup_medicine_composition
from pharmacy.services.price_predictor import predict_medicine_price
from pharmacy.forms import (
    PharmacyProfileForm,
    PharmacyResubmissionForm,
    MedicineForm,
    InventoryBatchCreateForm,
    InventoryBatchUpdateForm,
    SmartStockEntryForm,
    PharmacySupportRequestForm,
)
from pharmacy.services.voice_stock_parser import parse_voice_command
from pharmacy.services.catalogue_matcher import match_medicine_catalogue
from pharmacy.services.csv_stock_importer import (
    detect_csv_encoding_and_delimiter,
    read_csv_rows,
    auto_detect_column_mapping,
    validate_and_preview_csv,
    execute_csv_stock_import,
    generate_skipped_rows_csv,
    REQUIRED_CANONICAL_FIELDS,
)

logger = logging.getLogger(__name__)


@role_required('PHARMACY')
def dashboard_view(request):
    """
    Pharmacy Portal main dashboard.
    Accessible only to authenticated users with role='PHARMACY'
    and approval_status='APPROVED'.
    Displays real-time KPI metrics scoped strictly to the authenticated pharmacy.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)

    total_medicines = 0
    active_batches = 0
    low_stock_alerts = 0
    out_of_stock = 0

    if pharmacy_profile:
        today = timezone.localdate()
        base_inventory = Inventory.objects.filter(pharmacy=pharmacy_profile)

        # 1. Total Distinct Medicines in this pharmacy's inventory
        total_medicines = base_inventory.values('medicine_id').distinct().count()

        # 2. Active Inventory Batches (quantity > 0 and expiry_date >= today)
        active_batches = base_inventory.filter(
            quantity__gt=0,
            expiry_date__gte=today
        ).count()

        # 3. Low Stock Alerts (0 < quantity <= low_stock_limit and expiry_date >= today)
        low_stock_alerts = base_inventory.filter(
            quantity__gt=0,
            quantity__lte=F('low_stock_limit'),
            expiry_date__gte=today
        ).count()

        # 4. Out of Stock (quantity <= 0 and expiry_date >= today)
        out_of_stock = base_inventory.filter(
            quantity__lte=0,
            expiry_date__gte=today
        ).count()

    context = {
        'pharmacy_profile': pharmacy_profile,
        'total_medicines': total_medicines,
        'active_batches': active_batches,
        'low_stock_alerts': low_stock_alerts,
        'out_of_stock': out_of_stock,
    }
    return render(request, 'pharmacy/dashboard.html', context)


@role_required('PHARMACY')
def pharmacy_settings_view(request):
    """
    Pharmacy Profile Settings view.
    Allows authenticated, approved pharmacy owners to update their contact details,
    store information, and GPS coordinates.
    Derived exclusively from request.user.pharmacy_profile.
    Protected regulatory fields (license number, GSTIN, approval status) cannot be changed.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        messages.error(request, "Pharmacy profile not found.")
        return redirect('pharmacy:dashboard')

    if request.method == 'POST':
        form = PharmacyProfileForm(request.POST, instance=pharmacy_profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Pharmacy profile settings have been updated successfully.")
            return redirect('pharmacy:settings')
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        form = PharmacyProfileForm(instance=pharmacy_profile)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'form': form,
    }
    return render(request, 'pharmacy/settings.html', context)


settings_view = pharmacy_settings_view


@role_required('PHARMACY')
def medicines_list_view(request):
    """
    Master Medicine Catalogue Listing.
    Displays all available medicines with real-time search across name, composition,
    manufacturer, and strength, with clean pagination.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)

    query = request.GET.get('q', '').strip()
    medicines = Medicine.objects.all().order_by('medicine_name')

    if query:
        medicines = medicines.filter(
            Q(medicine_name__icontains=query) |
            Q(composition__icontains=query) |
            Q(manufacturer__icontains=query) |
            Q(strength__icontains=query)
        )

    paginator = Paginator(medicines, 12)  # 12 cards per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'page_obj': page_obj,
        'query': query,
        'total_count': paginator.count,
    }
    return render(request, 'pharmacy/medicines_list.html', context)


@role_required('PHARMACY')
def medicine_add_view(request):
    """
    Add a new medicine to the MedAccess master catalogue.
    Does NOT create an Inventory record.
    Prevents duplicate catalogue entries.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)

    if request.method == 'POST':
        form = MedicineForm(request.POST, request.FILES)
        if form.is_valid():
            medicine = form.save()
            messages.success(
                request,
                f"Medicine '{medicine.medicine_name}' has been added to the master catalogue successfully."
            )
            return redirect('pharmacy:medicines')
        else:
            messages.error(request, "Please review and correct the errors below.")
    else:
        form = MedicineForm()

    context = {
        'pharmacy_profile': pharmacy_profile,
        'form': form,
    }
    return render(request, 'pharmacy/medicine_form.html', context)


@role_required('PHARMACY')
def inventory_list_view(request):
    """
    Pharmacy Inventory & Stock Management Listing.
    Displays inventory batches belonging strictly to the authenticated pharmacy.
    Provides search by medicine name, composition, or batch number,
    plus filtering by stock status (In Stock, Low Stock, Out of Stock, Expired).
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    today = timezone.localdate()

    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', 'all').strip().lower()

    # Scope query strictly to the authenticated pharmacy
    batches = Inventory.objects.filter(pharmacy=pharmacy_profile).select_related('medicine')

    if query:
        batches = batches.filter(
            Q(medicine__medicine_name__icontains=query) |
            Q(medicine__composition__icontains=query) |
            Q(batch_number__icontains=query)
        )

    if status_filter == 'in_stock':
        batches = batches.filter(expiry_date__gte=today, quantity__gt=F('low_stock_limit'))
    elif status_filter == 'low_stock':
        batches = batches.filter(expiry_date__gte=today, quantity__gt=0, quantity__lte=F('low_stock_limit'))
    elif status_filter == 'out_of_stock':
        batches = batches.filter(expiry_date__gte=today, quantity__lte=0)
    elif status_filter == 'expired':
        batches = batches.filter(expiry_date__lt=today)

    batches = batches.order_by('expiry_date', 'medicine__medicine_name')

    # Pagination: 15 records per page
    paginator = Paginator(batches, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Attach computed status to each item on current page
    for batch in page_obj.object_list:
        if batch.expiry_date < today:
            batch.status_code = 'expired'
            batch.status_label = 'Expired'
            batch.status_badge_class = 'status-danger'
        elif batch.quantity <= 0:
            batch.status_code = 'out_of_stock'
            batch.status_label = 'Out of Stock'
            batch.status_badge_class = 'status-danger'
        elif batch.quantity <= batch.low_stock_limit:
            batch.status_code = 'low_stock'
            batch.status_label = 'Low Stock'
            batch.status_badge_class = 'status-warning'
        else:
            batch.status_code = 'in_stock'
            batch.status_label = 'In Stock'
            batch.status_badge_class = 'status-success'

    # Summary counts for quick status tabs
    base_qs = Inventory.objects.filter(pharmacy=pharmacy_profile)
    counts = {
        'all': base_qs.count(),
        'in_stock': base_qs.filter(expiry_date__gte=today, quantity__gt=F('low_stock_limit')).count(),
        'low_stock': base_qs.filter(expiry_date__gte=today, quantity__gt=0, quantity__lte=F('low_stock_limit')).count(),
        'out_of_stock': base_qs.filter(expiry_date__gte=today, quantity__lte=0).count(),
        'expired': base_qs.filter(expiry_date__lt=today).count(),
    }

    context = {
        'pharmacy_profile': pharmacy_profile,
        'page_obj': page_obj,
        'query': query,
        'status_filter': status_filter,
        'counts': counts,
        'today': today,
    }
    return render(request, 'pharmacy/inventory_list.html', context)


@role_required('PHARMACY')
def inventory_add_view(request):
    """
    Add a new inventory batch for an existing master catalogue medicine.
    Strictly scoped to the authenticated pharmacy.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)

    if request.method == 'POST':
        form = InventoryBatchCreateForm(request.POST, pharmacy_profile=pharmacy_profile)
        if form.is_valid():
            batch = form.save()
            messages.success(
                request,
                f"Batch '{batch.batch_number}' for '{batch.medicine.medicine_name}' added to inventory successfully."
            )
            return redirect('pharmacy:inventory')
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        initial = {}
        medicine_id = request.GET.get('medicine_id')
        if medicine_id and medicine_id.isdigit():
            initial['medicine'] = medicine_id
        form = InventoryBatchCreateForm(pharmacy_profile=pharmacy_profile, initial=initial)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'form': form,
    }
    return render(request, 'pharmacy/inventory_form.html', context)


@role_required('PHARMACY')
def inventory_update_view(request, pk):
    """
    Update stock quantity, expiry date, and stock limits for an existing batch.
    Strictly verifies ownership: get_object_or_404(Inventory, pk=pk, pharmacy=pharmacy_profile).
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    batch = get_object_or_404(Inventory, pk=pk, pharmacy=pharmacy_profile)

    if request.method == 'POST':
        form = InventoryBatchUpdateForm(request.POST, instance=batch)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                f"Stock for '{batch.medicine.medicine_name}' (Batch: {batch.batch_number}) updated successfully."
            )
            return redirect('pharmacy:inventory')
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        form = InventoryBatchUpdateForm(instance=batch)

    today = timezone.localdate()
    context = {
        'pharmacy_profile': pharmacy_profile,
        'batch': batch,
        'form': form,
        'today': today,
    }
    return render(request, 'pharmacy/inventory_update.html', context)


@role_required('PHARMACY')
@require_GET
def medicine_autocomplete_api(request):
    """
    Dedicated GET endpoint for searching the MedAccess Master Medicine Catalogue.
    Used by autocomplete widgets in inventory and prescription workflows.

    Query parameters:
        q (str): Search term across name, composition, manufacturer, or strength.
                 Requires at least 2 characters.

    Returns:
        JsonResponse containing up to 10 matching medicines with deterministic ordering.
    """
    query = (request.GET.get('q') or request.GET.get('query') or '').strip()

    if len(query) < 2:
        return JsonResponse({
            'status': 'success',
            'query': query,
            'count': 0,
            'results': [],
            'medicines': [],
        })

    matches = list(
        Medicine.objects.filter(
            Q(medicine_name__icontains=query) |
            Q(composition__icontains=query) |
            Q(manufacturer__icontains=query) |
            Q(strength__icontains=query)
        ).values(
            'id',
            'medicine_name',
            'composition',
            'manufacturer',
            'strength',
            'dosage_form',
            'pack_size',
            'mrp',
        ).order_by('medicine_name', 'id')[:10]
    )

    return JsonResponse({
        'status': 'success',
        'query': query,
        'count': len(matches),
        'results': matches,
        'medicines': matches,
    })


@role_required('PHARMACY')
def voice_stock_parse_api(request):
    """
    POST-only API for parsing voice transcripts into structured stock entry fields
    and matching candidate formulations from the Master Medicine Catalogue.
    Phase 2: Extracts parameters and queries catalogue. Does NOT create/update inventory.
    """
    if request.method != 'POST':
        return JsonResponse(
            {'success': False, 'error': 'Method not allowed. POST required.'},
            status=405
        )

    # Enforce approved pharmacy profile ownership check
    profile = getattr(request.user, 'pharmacy_profile', None)
    if not profile or profile.approval_status != 'APPROVED':
        return JsonResponse(
            {'success': False, 'error': 'Only approved pharmacy accounts may access voice parsing.'},
            status=403
        )

    # Extract transcript from JSON payload or standard form-encoded POST
    transcript = ''
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body.decode('utf-8'))
            if isinstance(data, dict):
                transcript = data.get('transcript', '')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {'success': False, 'error': 'Malformed JSON payload.'},
                status=400
            )
    else:
        transcript = request.POST.get('transcript', '')

    if not isinstance(transcript, str):
        return JsonResponse(
            {'success': False, 'error': 'Invalid transcript format. Text expected.'},
            status=400
        )

    clean_transcript = transcript.strip()
    if not clean_transcript:
        return JsonResponse(
            {'success': False, 'error': 'Voice transcript cannot be empty.'},
            status=400
        )

    if len(clean_transcript) > 1000:
        return JsonResponse(
            {'success': False, 'error': 'Transcript exceeds maximum allowed length of 1000 characters.'},
            status=400
        )

    # Parse voice command into structured parameters
    extracted = parse_voice_command(clean_transcript)

    # Query existing master medicine catalogue
    candidates = []
    if extracted.get('medicine_name'):
        candidates = match_medicine_catalogue(
            medicine_name=extracted['medicine_name'],
            strength=extracted.get('strength'),
            limit=6
        )

    return JsonResponse({
        'success': True,
        'transcript': clean_transcript,
        'extracted': extracted,
        'candidates': candidates,
        'match_count': len(candidates),
    })


@role_required('PHARMACY')
def csv_stock_analyze_api(request):
    """
    POST-only endpoint: Receives uploaded CSV file, inspects header row, detects delimiter & encoding,
    suggests column mapping based on synonym aliases, and returns sample rows.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed. POST required.'}, status=405)

    profile = getattr(request.user, 'pharmacy_profile', None)
    if not profile or profile.approval_status != 'APPROVED':
        return JsonResponse({'success': False, 'error': 'Approved pharmacy profile required.'}, status=403)

    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        return JsonResponse({'success': False, 'error': 'No CSV file provided.'}, status=400)

    if not csv_file.name.lower().endswith('.csv'):
        return JsonResponse({'success': False, 'error': 'Invalid file format. Please upload a .csv file.'}, status=400)

    # 10 MB file size limit
    if csv_file.size > 10 * 1024 * 1024:
        return JsonResponse({'success': False, 'error': 'File size exceeds maximum allowed limit of 10 MB.'}, status=400)

    try:
        file_bytes = csv_file.read()
        if not file_bytes:
            return JsonResponse({'success': False, 'error': 'The uploaded CSV file is empty.'}, status=400)

        encoding, delimiter, decoded_text = detect_csv_encoding_and_delimiter(file_bytes)
        headers, rows = read_csv_rows(decoded_text, delimiter)

        if not headers:
            return JsonResponse({'success': False, 'error': 'Could not detect valid headers in CSV file.'}, status=400)

        if len(rows) == 0:
            return JsonResponse({'success': False, 'error': 'CSV contains headers but no data rows.'}, status=400)

        if len(rows) > 5000:
            return JsonResponse({'success': False, 'error': f'CSV contains {len(rows)} rows, which exceeds the 5,000 row limit.'}, status=400)

        detected_mapping = auto_detect_column_mapping(headers)

        return JsonResponse({
            'success': True,
            'filename': csv_file.name,
            'file_size': csv_file.size,
            'encoding': encoding,
            'delimiter': delimiter,
            'headers': headers,
            'total_rows': len(rows),
            'detected_mapping': detected_mapping,
            'sample_rows': rows[:5],
        })
    except Exception as exc:
        return JsonResponse({'success': False, 'error': f'Error reading CSV: {str(exc)}'}, status=400)


@role_required('PHARMACY')
def csv_stock_validate_api(request):
    """
    POST-only endpoint: Validates CSV rows against confirmed column mapping,
    checks catalogue matches, verifies format of expiry/quantities,
    and identifies duplicate batches against DB and within the file.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed. POST required.'}, status=405)

    profile = getattr(request.user, 'pharmacy_profile', None)
    if not profile or profile.approval_status != 'APPROVED':
        return JsonResponse({'success': False, 'error': 'Approved pharmacy profile required.'}, status=403)

    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        return JsonResponse({'success': False, 'error': 'CSV file missing.'}, status=400)

    # Read mapping JSON string
    mapping_raw = request.POST.get('mapping')
    if not mapping_raw:
        return JsonResponse({'success': False, 'error': 'Column mapping configuration is required.'}, status=400)

    try:
        mapping = json.loads(mapping_raw) if isinstance(mapping_raw, str) else mapping_raw
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Malformed column mapping JSON.'}, status=400)

    # Check required fields are mapped
    for req_field in REQUIRED_CANONICAL_FIELDS:
        if not mapping.get(req_field):
            return JsonResponse({
                'success': False,
                'error': f"Required field '{req_field.replace('_', ' ').title()}' is not mapped to any CSV column."
            }, status=400)

    try:
        file_bytes = csv_file.read()
        encoding, delimiter, decoded_text = detect_csv_encoding_and_delimiter(file_bytes)
        headers, rows = read_csv_rows(decoded_text, delimiter)

        if len(rows) > 5000:
            return JsonResponse({'success': False, 'error': 'Row count exceeds maximum limit of 5,000 rows.'}, status=400)

        preview_data = validate_and_preview_csv(rows, mapping, profile)
        return JsonResponse({
            'success': True,
            **preview_data
        })
    except Exception as exc:
        return JsonResponse({'success': False, 'error': f'Validation failed: {str(exc)}'}, status=400)


@role_required('PHARMACY')
def csv_stock_import_api(request):
    """
    POST-only endpoint: Commits confirmed stock rows and approved new catalogue medicines
    into the master catalogue and the authenticated pharmacy's Inventory.
    Re-validates rows server-side and writes records transactionally.
    Returns success metrics and downloadable error report CSV.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed. POST required.'}, status=405)

    profile = getattr(request.user, 'pharmacy_profile', None)
    if not profile or profile.approval_status != 'APPROVED':
        return JsonResponse({'success': False, 'error': 'Approved pharmacy profile required.'}, status=403)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Malformed JSON body.'}, status=400)

    matched_rows = data.get('matched_rows')
    new_medicines = data.get('new_medicines', [])

    # Backward compatibility: if data has 'rows' instead of 'matched_rows'
    if matched_rows is None and 'rows' in data:
        matched_rows = data.get('rows', [])
    if matched_rows is None:
        matched_rows = []

    if not isinstance(matched_rows, list) or not isinstance(new_medicines, list):
        return JsonResponse({'success': False, 'error': 'Invalid rows data format.'}, status=400)

    total_candidates = len(matched_rows) + len(new_medicines)
    if total_candidates == 0:
        return JsonResponse({'success': False, 'error': 'No valid stock rows or new medicines provided for import.'}, status=400)

    if total_candidates > 5000:
        return JsonResponse({'success': False, 'error': 'Exceeds maximum import limit of 5,000 rows.'}, status=400)

    results = execute_csv_stock_import(matched_rows, profile, new_medicines=new_medicines)
    error_csv = generate_skipped_rows_csv(results['skipped_rows'] + results['failed_rows'])

    return JsonResponse({
        'success': True,
        'imported_count': results['imported_count'],
        'skipped_count': results['skipped_count'],
        'failed_count': results['failed_count'],
        'new_medicines_created_count': results['new_medicines_created_count'],
        'existing_medicines_imported_count': results['existing_medicines_imported_count'],
        'stock_batches_imported_count': results['stock_batches_imported_count'],
        'imported_rows': results['imported_rows'],
        'skipped_rows': results['skipped_rows'],
        'failed_rows': results['failed_rows'],
        'error_report_csv': error_csv,
    })


@role_required('PHARMACY')
def smart_stock_entry_view(request):
    """
    Update Stocks / Smart Stock Entry view.
    Dedicated interface for adding new stock to pharmacy inventory using
    autocomplete search over the 7,400+ master medicine catalogue.
    Validates and saves batches using existing inventory models and rules.
    Prepared for future voice-based stock entry.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    selected_medicine = None

    if request.method == 'POST':
        form = SmartStockEntryForm(request.POST, pharmacy_profile=pharmacy_profile)
        if form.is_valid():
            batch = form.save()
            messages.success(
                request,
                f"Stock batch '{batch.batch_number}' for '{batch.medicine.medicine_name}' added to inventory successfully."
            )
            return redirect('pharmacy:inventory')
        else:
            messages.error(request, "Please review and correct the errors below.")
            med_id = request.POST.get('medicine')
            if med_id and med_id.isdigit():
                selected_medicine = Medicine.objects.filter(pk=med_id).first()
    else:
        initial = {}
        medicine_id = request.GET.get('medicine_id')
        if medicine_id and medicine_id.isdigit():
            selected_medicine = Medicine.objects.filter(pk=medicine_id).first()
            if selected_medicine:
                initial['medicine'] = selected_medicine.pk
        form = SmartStockEntryForm(pharmacy_profile=pharmacy_profile, initial=initial)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'form': form,
        'selected_medicine': selected_medicine,
    }
    return render(request, 'pharmacy/smart_stock_entry.html', context)


update_stock_view = smart_stock_entry_view


@role_required('PHARMACY')
def customer_feedback_view(request):
    """
    Pharmacy Customer Feedback & Ratings Dashboard.
    Displays dynamic rating metrics, rating distribution, and patient reviews
    scoped strictly to the authenticated pharmacy owner's PharmacyProfile.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        messages.error(request, "Pharmacy profile not found. Please contact administration.")
        return redirect('landing')

    # Strict Data Isolation: Scope all feedback to this authenticated pharmacy
    base_feedback_qs = Feedback.objects.filter(pharmacy=pharmacy_profile)

    # 1. Lifetime summary metrics for this pharmacy
    total_reviews = base_feedback_qs.count()

    if total_reviews > 0:
        avg_aggregate = base_feedback_qs.aggregate(avg=Avg('rating'))['avg'] or 0.0
        avg_rating = round(float(avg_aggregate), 1)
    else:
        avg_rating = 0.0

    # Rating distribution breakdown (5-star down to 1-star)
    rating_counts = {i: 0 for i in range(1, 6)}
    distribution_aggregates = (
        base_feedback_qs.values('rating')
        .annotate(count=Count('id'))
    )
    for entry in distribution_aggregates:
        r = entry['rating']
        if r in rating_counts:
            rating_counts[r] = entry['count']

    rating_distribution = []
    for stars in range(5, 0, -1):
        count = rating_counts[stars]
        percentage = round((count / total_reviews * 100), 1) if total_reviews > 0 else 0.0
        rating_distribution.append({
            'stars': stars,
            'count': count,
            'percentage': percentage,
        })

    # 2. Filtering & Search on reviews list
    feedback_list = base_feedback_qs.select_related('patient__user')

    search_query = request.GET.get('q', '').strip()
    rating_filter = request.GET.get('rating', 'all').strip()

    if search_query:
        feedback_list = feedback_list.filter(comment__icontains=search_query)

    selected_rating = None
    if rating_filter in ['1', '2', '3', '4', '5']:
        selected_rating = int(rating_filter)
        feedback_list = feedback_list.filter(rating=selected_rating)

    # Ordering: Newest reviews first
    feedback_list = feedback_list.order_by('-created_at')

    # 3. Pagination: 10 reviews per page
    paginator = Paginator(feedback_list, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Attach privacy-conscious display name to each item on the current page
    for item in page_obj.object_list:
        patient_profile = getattr(item, 'patient', None)
        patient_user = getattr(patient_profile, 'user', None) if patient_profile else None
        if patient_user:
            fname = patient_user.first_name.strip() if patient_user.first_name else ''
            lname = patient_user.last_name.strip() if patient_user.last_name else ''
            if fname and lname:
                item.display_name = f"{fname} {lname[0]}."
            elif fname:
                item.display_name = fname
            elif patient_user.username:
                item.display_name = patient_user.username
            else:
                item.display_name = "Patient"
        else:
            item.display_name = "Patient"

    context = {
        'pharmacy_profile': pharmacy_profile,
        'total_reviews': total_reviews,
        'avg_rating': avg_rating,
        'rating_distribution': rating_distribution,
        'reviews': page_obj,
        'search_query': search_query,
        'rating_filter': rating_filter,
        'selected_rating': selected_rating,
        'has_filters': bool(search_query or (rating_filter and rating_filter != 'all')),
        'filtered_count': feedback_list.count(),
    }
    return render(request, 'pharmacy/customer_feedback.html', context)


@role_required('PHARMACY')
def analytics_view(request):
    """
    Pharmacy Portal Analytics & Reports page.
    Displays operational analytics metrics and hosts the dedicated
    Medicine Composition Lookup module powered by the Tata 1mg dataset.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    today = timezone.localdate()

    total_inventory_items = 0
    active_batches = 0
    low_stock_count = 0
    expired_batches = 0
    total_units_in_stock = 0
    total_feedback = 0
    avg_rating = 0.0

    if pharmacy_profile:
        base_inv = Inventory.objects.filter(pharmacy=pharmacy_profile)
        total_inventory_items = base_inv.values('medicine_id').distinct().count()
        active_batches = base_inv.filter(quantity__gt=0, expiry_date__gte=today).count()
        low_stock_count = base_inv.filter(
            quantity__gt=0,
            quantity__lte=F('low_stock_limit'),
            expiry_date__gte=today
        ).count()
        expired_batches = base_inv.filter(expiry_date__lt=today).count()
        sum_result = base_inv.filter(quantity__gt=0).aggregate(total=Sum('quantity'))
        total_units_in_stock = sum_result.get('total') or 0

        feedback_qs = Feedback.objects.filter(pharmacy=pharmacy_profile)
        total_feedback = feedback_qs.count()
        avg_res = feedback_qs.aggregate(avg=Avg('rating'))['avg']
        avg_rating = round(avg_res, 1) if avg_res else 0.0

    master_catalogue_count = Medicine.objects.count()
    verified_dataset_count = MedicineComposition.objects.count()

    # Support optional initial search via query parameter ?q=...
    initial_query = request.GET.get('q', '').strip()
    disambiguate_id = request.GET.get('id', '').strip()
    parsed_id = int(disambiguate_id) if disambiguate_id.isdigit() else None

    initial_lookup_result = None
    if initial_query or parsed_id:
        initial_lookup_result = lookup_medicine_composition(initial_query, disambiguate_id=parsed_id)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'total_inventory_items': total_inventory_items,
        'active_batches': active_batches,
        'low_stock_count': low_stock_count,
        'expired_batches': expired_batches,
        'total_units_in_stock': total_units_in_stock,
        'total_feedback': total_feedback,
        'avg_rating': avg_rating,
        'master_catalogue_count': master_catalogue_count,
        'verified_dataset_count': verified_dataset_count,
        'verified_dataset_count_display': f"{verified_dataset_count:,}",
        'initial_query': initial_query,
        'initial_lookup_result': initial_lookup_result,
    }
    return render(request, 'pharmacy/analytics.html', context)


@role_required('PHARMACY')
@require_GET
def composition_lookup_api(request):
    """
    JSON API for live verified medicine composition lookup.
    Used by the interactive Medicine Composition Lookup component
    on the Analytics & Reports page.
    """
    query = request.GET.get('q', '').strip()
    disambiguate_id = request.GET.get('id', '').strip()
    parsed_id = int(disambiguate_id) if disambiguate_id.isdigit() else None

    result = lookup_medicine_composition(query, disambiguate_id=parsed_id)
    return JsonResponse(result)


@role_required('PHARMACY')
@require_GET
def composition_autocomplete_api(request):
    """
    Real-time autocomplete suggestions for the Medicine Composition Lookup module.
    Queries distinct medicine names from the Tata 1mg MedicineComposition dataset.
    Requires at least 2 characters. Returns up to 10 suggestions.
    """
    query = (request.GET.get('q') or request.GET.get('term') or '').strip()

    if len(query) < 2:
        return JsonResponse({
            'status': 'success',
            'query': query,
            'count': 0,
            'suggestions': []
        })

    # 1. Prioritize prefix matching (utilizes indexed name field)
    names = list(
        MedicineComposition.objects.filter(name__istartswith=query)
        .values_list('name', flat=True)
        .distinct()[:10]
    )

    # 2. If fewer than 10 results, supplement with substring matches
    if len(names) < 10:
        remaining = 10 - len(names)
        substring_matches = list(
            MedicineComposition.objects.filter(name__icontains=query)
            .exclude(name__in=names)
            .values_list('name', flat=True)
            .distinct()[:remaining]
        )
        names.extend(substring_matches)

    return JsonResponse({
        'status': 'success',
        'query': query,
        'count': len(names),
        'suggestions': names
    })


@role_required('PHARMACY')
@require_POST
def price_prediction_api(request):
    """
    Dedicated POST JSON API for predicting estimated Maximum Retail Price (MRP).
    Powered by the L2-regularized Ridge Regression Model v2 (price_prediction_model_v2.pkl).

    Accepts JSON payload or POST form parameters:
        - name (str, required): Medicine brand name.
        - quantity (str, optional): Packaging specification (e.g. 'strip of 10 tablets').
        - manufacturer (str, optional): Pharma company name.
        - salt_composition (str, optional): Active salt formulation.
        - pack_size (float/int/str, required/optional): Units count in pack.

    Returns:
        JsonResponse with 200 on success, 400 on validation failure, 500 on server error.
    """
    data = {}
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON format in request payload.'
            }, status=400)
    else:
        data = request.POST.dict()

    name = data.get('name', '')
    quantity = data.get('quantity', '')
    manufacturer = data.get('manufacturer', '')
    salt_composition = data.get('salt_composition', '')
    pack_size = data.get('pack_size', None)

    try:
        result = predict_medicine_price(
            name=name,
            quantity=quantity,
            manufacturer=manufacturer,
            salt_composition=salt_composition,
            pack_size=pack_size
        )
    except Exception as e:
        logger.exception(f"Unexpected error in price_prediction_api: {e}")
        return JsonResponse({
            'success': False,
            'error': 'An internal error occurred while predicting medicine price.'
        }, status=500)

    if not result.get('success'):
        return JsonResponse(result, status=400)

    return JsonResponse(result, status=200)


# ============================================================================
# Phase 3: Pharmacy Request Inbox & Status Management Views
# ============================================================================

@role_required('PHARMACY')
def requests_inbox_view(request):
    """
    Pharmacy Request Inbox.
    Displays all incoming prescription fulfillment requests addressed strictly
    to the authenticated, approved pharmacy.
    Supports filtering by status tabs, text search, and pagination.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        raise Http404("Pharmacy profile not found.")

    status_filter = request.GET.get('status', 'all').strip().lower()
    query = request.GET.get('q', '').strip()

    # Base queryset strictly scoped to the logged-in pharmacy
    base_qs = (
        PrescriptionRequest.objects.filter(pharmacy=pharmacy_profile)
        .select_related('patient__user', 'prescription')
        .prefetch_related('items')
    )

    # Precompute tab counts for all statuses
    counts = {
        'all': base_qs.count(),
        'pending': base_qs.filter(status='PENDING').count(),
        'accepted': base_qs.filter(status='ACCEPTED').count(),
        'processing': base_qs.filter(status='PROCESSING').count(),
        'ready_for_pickup': base_qs.filter(status='READY_FOR_PICKUP').count(),
        'completed': base_qs.filter(status='COMPLETED').count(),
        'rejected': base_qs.filter(status='REJECTED').count(),
        'cancelled': base_qs.filter(status='CANCELLED').count(),
    }

    filtered_qs = base_qs
    if status_filter == 'pending':
        filtered_qs = filtered_qs.filter(status='PENDING')
    elif status_filter == 'accepted':
        filtered_qs = filtered_qs.filter(status='ACCEPTED')
    elif status_filter == 'processing':
        filtered_qs = filtered_qs.filter(status='PROCESSING')
    elif status_filter == 'ready_for_pickup':
        filtered_qs = filtered_qs.filter(status='READY_FOR_PICKUP')
    elif status_filter == 'completed':
        filtered_qs = filtered_qs.filter(status='COMPLETED')
    elif status_filter == 'rejected':
        filtered_qs = filtered_qs.filter(status='REJECTED')
    elif status_filter == 'cancelled':
        filtered_qs = filtered_qs.filter(status='CANCELLED')

    if query:
        if query.isdigit():
            filtered_qs = filtered_qs.filter(
                Q(id=int(query)) | Q(prescription_id=int(query))
            )
        else:
            filtered_qs = filtered_qs.filter(
                Q(patient__user__first_name__icontains=query) |
                Q(patient__user__last_name__icontains=query) |
                Q(patient__user__username__icontains=query) |
                Q(patient__user__phone_number__icontains=query)
            )

    filtered_qs = filtered_qs.order_by('-created_at')

    # Pagination: 15 requests per page
    paginator = Paginator(filtered_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'counts': counts,
        'query': query,
        'total_count': filtered_qs.count(),
    }
    return render(request, 'pharmacy/requests_inbox.html', context)


@role_required('PHARMACY')
def request_detail_view(request, pk):
    """
    Pharmacy Request Detail Workspace.
    Provides complete fulfillment context for a single prescription request:
    - Fulfillment-essential patient information
    - Secure prescription document link / viewer
    - Historical prescribed medicine snapshots vs current live pharmacy inventory
    - Contextual status transition controls
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        raise Http404("Pharmacy profile not found.")

    presc_request = get_object_or_404(
        PrescriptionRequest.objects.select_related('patient__user', 'prescription', 'pharmacy').prefetch_related('items'),
        pk=pk,
        pharmacy=pharmacy_profile
    )

    items = list(presc_request.items.all().order_by('id'))
    today = timezone.localdate()

    # Compute current live inventory for all matched catalogue medicines in this pharmacy
    matched_med_ids = [
        it.matched_catalogue_medicine_id
        for it in items
        if it.matched_catalogue_medicine_id is not None
    ]

    live_stock_map = {}
    if matched_med_ids:
        inv_rows = (
            Inventory.objects.filter(
                pharmacy=pharmacy_profile,
                medicine_id__in=matched_med_ids,
                expiry_date__gte=today,
                quantity__gt=0,
            )
            .values('medicine_id')
            .annotate(total_qty=Sum('quantity'))
        )
        for row in inv_rows:
            live_stock_map[row['medicine_id']] = int(row['total_qty'] or 0)

    for it in items:
        if it.matched_catalogue_medicine_id:
            it.current_live_stock = live_stock_map.get(it.matched_catalogue_medicine_id, 0)
            it.has_catalogue_match = True
        else:
            it.current_live_stock = None
            it.has_catalogue_match = False

    context = {
        'pharmacy_profile': pharmacy_profile,
        'prescription_request': presc_request,
        'items': items,
    }
    return render(request, 'pharmacy/request_detail.html', context)


@role_required('PHARMACY')
@require_POST
def request_update_status_view(request, pk):
    """
    Handles pharmacy status transitions for a prescription request.
    Allowed transitions:
        PENDING -> ACCEPTED | REJECTED
        ACCEPTED -> PROCESSING
        PROCESSING -> READY_FOR_PICKUP
        READY_FOR_PICKUP -> COMPLETED

    Validations:
        - Must be an approved pharmacy account.
        - Scoped strictly to the logged-in pharmacy profile.
        - If REJECTED, pharmacy_notes is mandatory (>= 5 non-whitespace chars).
        - Atomic transaction with select_for_update() to prevent race conditions.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        raise Http404("Pharmacy profile not found.")

    target_status = request.POST.get('status', '').strip().upper()
    pharmacy_notes = request.POST.get('pharmacy_notes', '').strip()

    VALID_PHARMACY_TRANSITIONS = {
        'PENDING': ['ACCEPTED', 'REJECTED'],
        'ACCEPTED': ['PROCESSING'],
        'PROCESSING': ['READY_FOR_PICKUP'],
        'READY_FOR_PICKUP': ['COMPLETED'],
    }

    with transaction.atomic():
        try:
            presc_request = PrescriptionRequest.objects.select_for_update().select_related(
                'patient__user', 'pharmacy__user'
            ).get(
                pk=pk,
                pharmacy=pharmacy_profile
            )
        except PrescriptionRequest.DoesNotExist:
            raise Http404("Prescription request not found.")

        allowed_targets = VALID_PHARMACY_TRANSITIONS.get(presc_request.status, [])
        if target_status not in allowed_targets:
            messages.error(
                request,
                f"Invalid status change: cannot transition request from {presc_request.get_status_display()} to {target_status}."
            )
            return redirect('pharmacy:request_detail', pk=pk)

        # Mandatory rejection note validation (>= 5 non-whitespace characters)
        if target_status == 'REJECTED':
            if len(pharmacy_notes) < 5:
                messages.error(
                    request,
                    "A meaningful rejection reason (at least 5 characters) is required when rejecting a request."
                )
                return redirect('pharmacy:request_detail', pk=pk)
            presc_request.pharmacy_notes = pharmacy_notes
        else:
            # For other valid transitions, update notes if provided; preserve existing note if blank
            if pharmacy_notes:
                presc_request.pharmacy_notes = pharmacy_notes

        presc_request.status = target_status
        presc_request.save(update_fields=['status', 'pharmacy_notes', 'updated_at'])

        STATUS_NOTIFICATION_MAP = {
            'ACCEPTED': {
                'type': 'REQUEST_ACCEPTED',
                'title': 'Request Accepted',
                'message': f"Your prescription request #{presc_request.id} has been accepted by {pharmacy_profile.pharmacy_name}."
            },
            'REJECTED': {
                'type': 'REQUEST_REJECTED',
                'title': 'Request Rejected',
                'message': (
                    f"Your prescription request #{presc_request.id} was rejected by {pharmacy_profile.pharmacy_name}. "
                    f"Reason: {presc_request.pharmacy_notes}"
                )
            },
            'PROCESSING': {
                'type': 'REQUEST_PROCESSING',
                'title': 'Request Processing',
                'message': f"Your prescription request #{presc_request.id} is now being prepared by {pharmacy_profile.pharmacy_name}."
            },
            'READY_FOR_PICKUP': {
                'type': 'REQUEST_READY',
                'title': 'Ready for Pickup',
                'message': f"Your prescription order #{presc_request.id} is packed and ready for pickup at {pharmacy_profile.pharmacy_name}."
            },
            'COMPLETED': {
                'type': 'REQUEST_COMPLETED',
                'title': 'Request Completed',
                'message': f"Prescription request #{presc_request.id} has been completed by {pharmacy_profile.pharmacy_name}. Thank you!"
            },
        }

        notif_config = STATUS_NOTIFICATION_MAP.get(target_status)
        if notif_config:
            create_request_notification(
                request=presc_request,
                recipient=presc_request.patient.user,
                notification_type=notif_config['type'],
                title=notif_config['title'],
                message=notif_config['message'],
                actor=request.user
            )

    messages.success(
        request,
        f"Request #{presc_request.id} status successfully updated to {presc_request.get_status_display()}."
    )
    return redirect('pharmacy:request_detail', pk=pk)


@login_required
def pharmacy_resubmit_view(request):
    """
    Pharmacy Resubmission Portal.
    Allows a rejected or pending pharmacy owner to review any admin rejection notes,
    correct their regulatory and business credentials, and resubmit their profile.
    Submitting safely resets approval_status to 'PENDING' and is_approved to False.
    """
    if request.user.role != 'PHARMACY':
        messages.error(request, "Access restricted to registered pharmacy accounts.")
        return redirect('landing')

    profile = getattr(request.user, 'pharmacy_profile', None)
    if not profile:
        messages.error(request, "Pharmacy profile not found.")
        return redirect('landing')

    if profile.approval_status == 'APPROVED':
        messages.info(request, "Your pharmacy account is approved and active.")
        return redirect('pharmacy:dashboard')

    if request.method == 'POST':
        form = PharmacyResubmissionForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Your pharmacy registration details have been updated and resubmitted for admin verification."
            )
            return redirect('pharmacy:resubmit')
    else:
        form = PharmacyResubmissionForm(instance=profile)

    context = {
        'form': form,
        'profile': profile,
        'rejection_reason': profile.rejection_reason,
        'is_rejected': profile.approval_status == 'REJECTED',
        'is_pending': profile.approval_status == 'PENDING',
    }
    return render(request, 'pharmacy/resubmit.html', context)


PHARMACY_SUPPORT_FAQS = [
    {
        'slug': 'account-profile',
        'title': 'Account & Profile',
        'items': [
            {
                'id': 'faq-register-access',
                'question': 'How do I register and access my pharmacy account?',
                'keywords': 'register signup approval pending verification email drug license gstin login',
                'answer': (
                    'Pharmacies register through the Pharmacy Registration form with store details, '
                    'owner and pharmacist names, Drug License Number, and GSTIN. After registering, '
                    'you must click the email verification link sent to your registered email address '
                    '(or request a new link from Resend Verification Email). Once your email is verified, '
                    'Central Administration reviews your application: when approved, logging in opens '
                    'your Pharmacy Portal dashboard.'
                ),
            },
            {
                'id': 'faq-update-profile',
                'question': 'How can I update my pharmacy profile?',
                'keywords': 'profile settings edit address phone email gps coordinates latitude longitude license gst',
                'answer': (
                    'Open Settings from the Pharmacy Portal sidebar to update your pharmacy name, owner '
                    'name, registered pharmacist name, phone number, email address, street address, city, '
                    'state, PIN code, and GPS latitude/longitude coordinates. For regulatory compliance, '
                    'Drug License Number, GST Identification Number, and Approval Status are read-only; '
                    'submit a support request here if statutory details require an administrative update.'
                ),
            },
            {
                'id': 'faq-login-issue',
                'question': 'What should I do if I face a login issue?',
                'keywords': 'login error password reset verification email rejected resubmit pending',
                'answer': (
                    'Verify your username and password first, or use the Forgot Password reset link on the '
                    'login page. If login indicates your email is not verified, click Resend Verification '
                    'Email to receive a fresh activation link. If your application is PENDING, portal '
                    'access waits for administrator approval; if REJECTED, use the resubmission link shown '
                    'on login to review administrator feedback and update your registration.'
                ),
            },
        ],
    },
    {
        'slug': 'prescription-management',
        'title': 'Prescription Management',
        'items': [
            {
                'id': 'faq-view-requests',
                'question': 'How can I view prescription requests?',
                'keywords': 'requests inbox pending accepted processing ready pickup completed filter search',
                'answer': (
                    'Select Prescription Requests in the sidebar to view all fulfillment requests sent to '
                    'your pharmacy. You can filter requests by status tabs (All, Pending, Accepted, '
                    'Preparing/Processing, Ready for Pickup, Completed, Rejected, Cancelled) or search '
                    'by Request ID, Prescription ID, patient name, username, or phone number.'
                ),
            },
            {
                'id': 'faq-review-prescription',
                'question': "How do I review a patient's prescription?",
                'keywords': 'review prescription patient allergies chat document live stock inventory match',
                'answer': (
                    'Click any request in the Prescription Requests inbox to open its Request Detail '
                    'workspace. You can review patient fulfillment details (contact phone, address, known '
                    'drug allergies, and special instructions), view the original prescription image or PDF, '
                    'message the patient in real time via Chat with Patient, and compare requested medicines '
                    'against your live non-expired inventory counts.'
                ),
            },
            {
                'id': 'faq-accept-reject',
                'question': 'How can I accept or reject a prescription request?',
                'keywords': 'accept reject decline status transition note reason processing ready completed',
                'answer': (
                    'On a Pending request detail page, use the status action controls to Accept or Reject '
                    'the request. Rejecting a request requires entering a Pharmacy Note (at least 5 characters) '
                    'explaining the reason for the patient. After accepting, advance the request step-by-step '
                    'through Processing, Ready for Pickup, and Completed.'
                ),
            },
            {
                'id': 'faq-check-details',
                'question': 'How can I check the details of a prescription?',
                'keywords': 'details medicines dosage strength frequency duration quantity snapshot availability',
                'answer': (
                    'Inside the Request Detail page, the Prescription Document card displays the uploaded '
                    'image or authenticated PDF link, while the requested items table lists each medicine '
                    'name, strength, dosage, frequency, duration, instructions, initial availability '
                    'snapshot, and current live stock in your pharmacy.'
                ),
            },
        ],
    },
    {
        'slug': 'technical-issues',
        'title': 'Technical Issues',
        'items': [
            {
                'id': 'faq-image-loading',
                'question': 'What should I do if a prescription image is not loading?',
                'keywords': 'image broken pdf not loading archived storage permission 403 404 document',
                'answer': (
                    'Prescription files are protected behind an authenticated document endpoint accessible '
                    'only to the patient owner and pharmacies with a valid request. Try clicking Open Full '
                    'Size (or View Authenticated PDF for PDF prescriptions) and confirm your login session '
                    'is active. If a legacy or archived document is unavailable on storage, the extracted '
                    'medicine list and request details remain accessible below, and you can report the '
                    'Request ID using the support form.'
                ),
            },
            {
                'id': 'faq-inventory-stock',
                'question': 'Why is a medicine showing out of stock in a prescription request?',
                'keywords': 'stock batch expiry expired quantity update inventory csv voice smart stock',
                'answer': (
                    'Live availability counts on Prescription Requests only include batches matching the '
                    'catalogue medicine with quantity greater than 0 and an expiry date today or later. '
                    'Open Manage Inventory or Update Stock (which supports quick entry, voice parsing, and '
                    'CSV bulk import) to verify batch quantities and expiry dates.'
                ),
            },
        ],
    },
    {
        'slug': 'general-support',
        'title': 'General Support',
        'items': [
            {
                'id': 'faq-manage-activities',
                'question': 'How can I manage my pharmacy activities?',
                'keywords': 'dashboard kpi inventory medicines update stock analytics feedback requests',
                'answer': (
                    'Your Dashboard tracks distinct medicines, active batches, low-stock alerts, and '
                    'out-of-stock counts. From the sidebar you can process orders in Prescription Requests, '
                    'manage catalogue entries in Manage Medicines, maintain batch expiry/quantities in '
                    'Manage Inventory and Update Stock, monitor trends in Analytics & Reports, and read '
                    'patient reviews in Customer Feedback.'
                ),
            },
            {
                'id': 'faq-contact-support',
                'question': 'How can I contact the MedAccess support team?',
                'keywords': 'contact support request ticket form help team category subject message',
                'answer': (
                    'Complete the Contact Support form on this page by choosing an Issue Category, entering '
                    'a clear Subject and Message, and clicking Submit Support Request. Your submission is '
                    'securely saved to the MedAccess support queue and automatically linked to your logged-in '
                    'pharmacy profile.'
                ),
            },
        ],
    },
]


@role_required('PHARMACY')
def help_support_view(request):
    """
    Pharmacy Portal Help & Support center.
    Provides searchable, categorized FAQs grounded in verified Pharmacy Portal workflows,
    useful portal navigation links, and a CSRF-protected, database-backed support request form
    strictly scoped to the authenticated, approved pharmacy profile.
    """
    pharmacy_profile = getattr(request.user, 'pharmacy_profile', None)
    if not pharmacy_profile:
        raise Http404("Pharmacy profile not found.")

    if request.method == 'POST':
        form = PharmacySupportRequestForm(request.POST)
        if form.is_valid():
            support_request = form.save(commit=False)
            support_request.pharmacy = pharmacy_profile
            support_request.submitted_by = request.user
            support_request.save()
            messages.success(
                request,
                f"Your support request (#{support_request.id}) has been submitted successfully."
            )
            return redirect('pharmacy:help_support')
        else:
            messages.error(request, "Please correct the highlighted errors in your support request.")
    else:
        form = PharmacySupportRequestForm()

    recent_requests = PharmacySupportRequest.objects.filter(pharmacy=pharmacy_profile)[:5]
    total_faq_count = sum(len(cat['items']) for cat in PHARMACY_SUPPORT_FAQS)

    context = {
        'pharmacy_profile': pharmacy_profile,
        'form': form,
        'faq_categories': PHARMACY_SUPPORT_FAQS,
        'total_faq_count': total_faq_count,
        'recent_requests': recent_requests,
    }
    return render(request, 'pharmacy/help_support.html', context)


