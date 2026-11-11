import json
import logging
import math
import urllib.request
import urllib.parse
import urllib.error
from decimal import Decimal, InvalidOperation
import mimetypes
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.http import JsonResponse, Http404, FileResponse
from django.views.decorators.http import require_POST
from django.core.cache import cache

from accounts.decorators import role_required
from patient.models import (
    PatientProfile,
    Prescription,
    SavedPharmacy,
    PrescriptionMedicine,
    PrescriptionRequest,
    PrescriptionRequestItem,
)
from pharmacy.services.availability_engine import check_prescription_pharmacy_availability
from patient.forms import (
    PatientUserForm,
    PatientProfileForm,
    PrescriptionUploadForm,
    PrescriptionMedicineForm,
    ConfirmExtractedMedicineForm,
    PatientFeedbackForm,
)
from pharmacy.models import PharmacyProfile, Feedback
from patient.services.prescription_processor import process_prescription_file, PrescriptionProcessingError
from patient.services.gemini_vision import (
    extract_prescription_details,
    validate_extracted_prescription_data,
    GeminiVisionError,
    redact_sensitive_text,
)
from patient.services.notification_service import create_request_notification

logger = logging.getLogger(__name__)


@role_required('PATIENT')
def dashboard_view(request):
    """
    Patient Portal main dashboard.
    Displays real stats and recent prescriptions for the logged-in patient.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile and getattr(request.user, 'role', '') == 'PATIENT':
        patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    # --- Dashboard statistics ---
    total_prescriptions = 0
    saved_pharmacies_count = 0
    recent_prescriptions = []
    profile_completion = 0

    if patient_profile:
        total_prescriptions = patient_profile.prescriptions.count()
        saved_pharmacies_count = patient_profile.saved_pharmacies.count()
        recent_prescriptions = patient_profile.prescriptions.order_by('-uploaded_at')[:3]

        # Profile completion: 10 actual fields (4 User + 6 PatientProfile)
        core_fields = [
            request.user.first_name,
            request.user.last_name,
            request.user.email,
            request.user.phone_number,
            patient_profile.date_of_birth,
            patient_profile.gender,
            patient_profile.address,
            patient_profile.profile_photo,
            patient_profile.blood_group,
            patient_profile.allergies,
        ]
        filled_count = sum(1 for field in core_fields if field)
        profile_completion = int((filled_count / len(core_fields)) * 100)

    context = {
        'patient_profile': patient_profile,
        'total_prescriptions': total_prescriptions,
        'saved_pharmacies_count': saved_pharmacies_count,
        'recent_prescriptions': recent_prescriptions,
        'profile_completion': profile_completion,
    }
    return render(request, 'patient/dashboard.html', context)


@role_required('PATIENT')
def profile_view(request):
    """
    View and update patient profile information.
    Ensures a patient can only edit their own profile.
    """
    patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        user_form = PatientUserForm(request.POST, instance=request.user)
        profile_form = PatientProfileForm(request.POST, request.FILES, instance=patient_profile)

        if user_form.is_valid() and profile_form.is_valid():
            with transaction.atomic():
                user_form.save()
                profile_form.save()
            messages.success(request, 'Your profile details have been saved successfully.')
            return redirect('patient:profile')
        else:
            messages.error(request, 'Please correct the errors indicated below.')
    else:
        user_form = PatientUserForm(instance=request.user)
        profile_form = PatientProfileForm(instance=patient_profile)

    context = {
        'user_form': user_form,
        'profile_form': profile_form,
        'patient_profile': patient_profile,
    }
    return render(request, 'patient/profile.html', context)


@role_required('PATIENT')
@require_POST
def update_location_view(request):
    """
    Receives browser GPS coordinates via AJAX/Fetch and saves to PatientProfile.
    """
    try:
        data = json.loads(request.body)
        raw_lat = data.get('latitude')
        raw_lng = data.get('longitude')

        if raw_lat is None or raw_lng is None:
            return JsonResponse({'status': 'error', 'message': 'Missing coordinates.'}, status=400)

        # Convert to Decimal for precise coordinate storage
        latitude = Decimal(str(raw_lat)).quantize(Decimal('0.0000001'))
        longitude = Decimal(str(raw_lng)).quantize(Decimal('0.0000001'))

        # Ensure latitude (-90 to 90) and longitude (-180 to 180) are within valid ranges
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return JsonResponse({'status': 'error', 'message': 'Coordinates are out of valid range.'}, status=400)

        patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)
        patient_profile.current_latitude = latitude
        patient_profile.current_longitude = longitude
        patient_profile.location_updated_at = timezone.now()
        patient_profile.save(update_fields=['current_latitude', 'current_longitude', 'location_updated_at'])

        return JsonResponse({
            'status': 'success',
            'message': 'Location coordinates updated successfully.',
            'latitude': str(patient_profile.current_latitude),
            'longitude': str(patient_profile.current_longitude),
            'updated_at': patient_profile.location_updated_at.strftime('%b %d, %Y at %I:%M %p')
        })
    except (ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid coordinate payload.'}, status=400)



@role_required('PATIENT')
def upload_prescription_view(request):
    """
    Handles uploading a doctor's prescription (image or PDF) for the authenticated patient,
    running pre-processing and Gemini Vision extraction to extract medicines into structured data.
    """
    patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = PrescriptionUploadForm(request.POST, request.FILES)
        if form.is_valid():
            # Step 2 & 3: Save original file with status UPLOADED
            prescription = form.save(commit=False)
            prescription.patient = patient_profile
            prescription.status = 'UPLOADED'
            prescription.extracted_data = {'medicines': [], 'unclear_details': []}
            prescription.save()

            # Step 4: Set status to PROCESSING before beginning extraction
            prescription.status = 'PROCESSING'
            prescription.save(update_fields=['status'])

            # Step 5 - 9: Process file & execute Gemini Vision extraction
            try:
                # Step 5: Pass storage-opened file through Task 3A file processor (works for local and S3 storage)
                with prescription.prescription_image.open('rb') as rx_file:
                    processed = process_prescription_file(rx_file)

                # Step 6: Extract structured prescription details via Task 3B service
                extraction_result = extract_prescription_details(processed)

                # Step 7: Validate returned structured data
                validated_payload = validate_extracted_prescription_data({
                    'medicines': extraction_result.medicines,
                    'unclear_details': extraction_result.unclear_details,
                })

                # Step 8: Save validated JSON
                prescription.extracted_data = validated_payload

                # Step 9: Update status to COMPLETED
                prescription.status = 'COMPLETED'
                prescription.save(update_fields=['extracted_data', 'status'])

                messages.success(
                    request,
                    'Prescription uploaded and analyzed successfully. You can now view it and add your prescribed medicines.'
                )
            except Exception as exc:
                # Safe failure handling: Never leak credentials, raw errors, or stack traces
                logger.warning(
                    "Prescription extraction failed for prescription #%s: %s",
                    prescription.id,
                    redact_sensitive_text(str(exc))
                )
                prescription.status = 'FAILED'
                prescription.extracted_data = {'medicines': [], 'unclear_details': []}
                prescription.save(update_fields=['extracted_data', 'status'])

                messages.warning(
                    request,
                    'Prescription uploaded successfully, but automatic medicine extraction was unsuccessful. '
                    'You can manually enter your prescribed medicines.'
                )

            return redirect('patient:dashboard')
        else:
            messages.error(request, 'Please select a valid prescription file within the 10MB size limit.')
    else:
        form = PrescriptionUploadForm()

    context = {
        'form': form,
        'patient_profile': patient_profile,
    }
    return render(request, 'patient/upload_prescription.html', context)



def enrich_prescription_details(prescription):
    """
    Categorizes medicines for a prescription into:
    - unconfirmed_extracted: AI-extracted suggestions from extracted_data['medicines'] not yet confirmed
    - confirmed_medicines: PrescriptionMedicine records with source='AI_CONFIRMED'
    - manual_medicines: PrescriptionMedicine records with source='MANUAL'
    Computes confirmation progress counts and summary badge text.
    """
    all_saved = list(prescription.medicines.all().order_by('created_at', 'id'))
    confirmed_meds = [m for m in all_saved if m.source == 'AI_CONFIRMED']
    manual_meds = [m for m in all_saved if m.source == 'MANUAL']

    confirmed_indices = {m.extracted_index for m in confirmed_meds if m.extracted_index is not None}

    unconfirmed_extracted = []
    all_extracted = []
    unclear_details = []

    if prescription.status == 'COMPLETED' and isinstance(prescription.extracted_data, dict):
        raw_medicines = prescription.extracted_data.get('medicines')
        if isinstance(raw_medicines, list):
            for idx, raw_item in enumerate(raw_medicines):
                if isinstance(raw_item, dict):
                    name = str(raw_item.get('medicine_name', '')).strip()
                    item_dict = {
                        'index': idx,
                        'medicine_name': name,
                        'strength': str(raw_item.get('strength', '')).strip(),
                        'dosage': str(raw_item.get('dosage', '')).strip(),
                        'frequency': str(raw_item.get('frequency', '')).strip(),
                        'duration': str(raw_item.get('duration', '')).strip(),
                        'instructions': str(raw_item.get('instructions', '')).strip(),
                        'needs_verification': bool(raw_item.get('needs_verification', False)),
                        'verification_reason': str(raw_item.get('verification_reason', '')).strip(),
                        'is_confirmed': idx in confirmed_indices,
                    }
                elif isinstance(raw_item, str) and raw_item.strip():
                    name = raw_item.strip()
                    item_dict = {
                        'index': idx,
                        'medicine_name': name,
                        'strength': '',
                        'dosage': '',
                        'frequency': '',
                        'duration': '',
                        'instructions': '',
                        'needs_verification': False,
                        'verification_reason': '',
                        'is_confirmed': idx in confirmed_indices,
                    }
                else:
                    continue

                all_extracted.append(item_dict)
                if not item_dict['is_confirmed']:
                    unconfirmed_extracted.append(item_dict)

        raw_unclear = prescription.extracted_data.get('unclear_details')
        if isinstance(raw_unclear, list):
            for u in raw_unclear:
                if isinstance(u, str) and u.strip():
                    unclear_details.append(u.strip())

    total_ai = len(all_extracted)
    confirmed_ai = len(confirmed_indices)

    if total_ai > 0:
        if confirmed_ai == total_ai:
            confirmation_summary = "All available medicines reviewed"
            confirmation_status = "all_reviewed"
        elif confirmed_ai > 0:
            confirmation_summary = f"{confirmed_ai} of {total_ai} medicines confirmed"
            confirmation_status = "partial"
        else:
            confirmation_summary = "Review pending"
            confirmation_status = "pending"
    elif prescription.status == 'COMPLETED':
        confirmation_summary = "No medicines extracted"
        confirmation_status = "none_extracted"
    else:
        confirmation_summary = ""
        confirmation_status = "not_ready"

    return {
        'confirmed_medicines': confirmed_meds,
        'manual_medicines': manual_meds,
        'unconfirmed_extracted': unconfirmed_extracted,
        'all_extracted': all_extracted,
        'unclear_details': unclear_details,
        'total_ai_count': total_ai,
        'confirmed_ai_count': confirmed_ai,
        'confirmation_summary': confirmation_summary,
        'confirmation_status': confirmation_status,
    }


@role_required('PATIENT')
def prescription_list_view(request):
    """
    Displays all prescriptions belonging to the authenticated patient, newest first.
    Enriches each prescription in-memory with file format indicators, previews, categorized medicines, and confirmation state.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    prescriptions = []
    if patient_profile:
        prescriptions = patient_profile.prescriptions.order_by('-uploaded_at')
        for rx in prescriptions:
            # File format detection (PDF vs Image) without remote HEAD calls per item
            rx.is_pdf = bool(
                rx.prescription_image and rx.prescription_image.name.lower().endswith('.pdf')
            )

            # Categorized medicines and confirmation progress
            summary = enrich_prescription_details(rx)
            rx.confirmed_medicines = summary['confirmed_medicines']
            rx.manual_medicines = summary['manual_medicines']
            rx.unconfirmed_extracted = summary['unconfirmed_extracted']
            rx.all_extracted = summary['all_extracted']
            rx.extracted_medicines = summary['all_extracted']  # Backward compatibility for existing modal tests
            rx.unclear_details = summary['unclear_details']
            rx.confirmation_summary = summary['confirmation_summary']
            rx.confirmation_status = summary['confirmation_status']
            rx.total_ai_count = summary['total_ai_count']
            rx.confirmed_ai_count = summary['confirmed_ai_count']

            # Extracted medicine previews for card chips
            med_names = [m['medicine_name'] for m in rx.all_extracted if m.get('medicine_name')]
            rx.preview_medicines = med_names[:3]
            rx.more_medicines_count = max(0, len(med_names) - 3)

    context = {
        'patient_profile': patient_profile,
        'prescriptions': prescriptions,
    }
    return render(request, 'patient/prescriptions_list.html', context)


@role_required('PATIENT')
def prescription_detail_view(request, pk):
    """
    Displays details of a single prescription.
    Security: Scoped by patient profile so patients can only view their own records.
    Provides categorized sections: AI Extracted, Patient Confirmed, and Manually Added.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)
    try:
        prescription.file_exists = bool(
            prescription.prescription_image and prescription.prescription_image.storage.exists(prescription.prescription_image.name)
        )
    except Exception:
        prescription.file_exists = False
    summary = enrich_prescription_details(prescription)
    prescription.confirmed_medicines = summary['confirmed_medicines']
    prescription.manual_medicines = summary['manual_medicines']
    prescription.unconfirmed_extracted = summary['unconfirmed_extracted']
    prescription.all_extracted = summary['all_extracted']
    prescription.unclear_details = summary['unclear_details']
    prescription.confirmation_summary = summary['confirmation_summary']
    prescription.confirmation_status = summary['confirmation_status']
    prescription.total_ai_count = summary['total_ai_count']
    prescription.confirmed_ai_count = summary['confirmed_ai_count']

    medicines = prescription.medicines.all().order_by('created_at', 'id')
    medicine_form = PrescriptionMedicineForm()

    context = {
        'patient_profile': patient_profile,
        'prescription': prescription,
        'medicines': medicines,
        'confirmed_medicines': summary['confirmed_medicines'],
        'manual_medicines': summary['manual_medicines'],
        'unconfirmed_extracted': summary['unconfirmed_extracted'],
        'all_extracted': summary['all_extracted'],
        'unclear_details': summary['unclear_details'],
        'confirmation_summary': summary['confirmation_summary'],
        'confirmation_status': summary['confirmation_status'],
        'total_ai_count': summary['total_ai_count'],
        'confirmed_ai_count': summary['confirmed_ai_count'],
        'medicine_form': medicine_form,
    }
    return render(request, 'patient/prescription_detail.html', context)


@role_required('PATIENT')
@require_POST
def confirm_prescription_medicine_view(request, pk):
    """
    Allows patient to manually enter and save a prescribed medicine.
    Ensures source='MANUAL' and extracted_index=None.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)
    form = PrescriptionMedicineForm(request.POST)

    if form.is_valid():
        medicine = form.save(commit=False)
        medicine.prescription = prescription
        medicine.source = 'MANUAL'
        medicine.extracted_index = None
        medicine.save()
        messages.success(request, f'Medicine "{medicine.medicine_name}" added successfully.')
    else:
        error_msgs = []
        for field, errors in form.errors.items():
            for error in errors:
                error_msgs.append(f"{field.replace('_', ' ').title()}: {error}")
        messages.error(request, f"Please correct the errors: {', '.join(error_msgs)}")

    next_url = request.POST.get('next')
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    return redirect('patient:prescription_detail', pk=prescription.pk)


@role_required('PATIENT')
@require_POST
def confirm_extracted_medicine_view(request, pk):
    """
    Allows patient to review, edit, and confirm an individual Gemini-extracted medicine.
    Validates against the stored prescription.extracted_data JSON.
    Enforces acknowledgment for uncertain items (needs_verification=True).
    Saves to PrescriptionMedicine with source='AI_CONFIRMED' and extracted_index.
    Supports both asynchronous JSON response and standard form POST redirect.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)

    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        'application/json' in request.headers.get('accept', '')
    )

    if prescription.status != 'COMPLETED':
        err_msg = "Cannot confirm medicines for a prescription that has not completed analysis."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    # Validate extracted_index parameter
    raw_index = request.POST.get('extracted_index')
    try:
        extracted_index = int(raw_index)
        if extracted_index < 0:
            raise ValueError()
    except (TypeError, ValueError):
        err_msg = "Invalid extracted medicine reference."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    # Server-side validation against saved AI extraction
    raw_data = prescription.extracted_data if isinstance(prescription.extracted_data, dict) else {}
    raw_meds = raw_data.get('medicines') if isinstance(raw_data.get('medicines'), list) else []

    if extracted_index >= len(raw_meds):
        err_msg = f"Extracted medicine #{extracted_index} does not exist on this prescription."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    original_item = raw_meds[extracted_index]
    if not isinstance(original_item, dict):
        original_item = {'medicine_name': str(original_item), 'needs_verification': False}

    # Duplicate prevention: check existing record with this extracted_index
    if prescription.medicines.filter(extracted_index=extracted_index).exists():
        err_msg = "This extracted medicine has already been confirmed."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.warning(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    # Uncertainty validation: require explicit acknowledgment when needs_verification is True
    needs_verification = bool(original_item.get('needs_verification', False))
    ack_val = request.POST.get('uncertainty_acknowledged')
    acknowledged = ack_val in ('true', '1', 'on', True)

    if needs_verification and not acknowledged:
        err_msg = "Please acknowledge the verification warning before confirming this medicine."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    # Validate patient-reviewed and edited inputs
    medicine_name = request.POST.get('medicine_name', '').strip()
    if not medicine_name:
        err_msg = "Medicine name is required."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    if len(medicine_name) > 255:
        err_msg = "Medicine name must be 255 characters or fewer."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    strength = request.POST.get('strength', '').strip()[:100] or None
    dosage = request.POST.get('dosage', '').strip()[:255] or None
    frequency = request.POST.get('frequency', '').strip()[:100] or None
    duration = request.POST.get('duration', '').strip()[:100] or None
    instructions = request.POST.get('instructions', '').strip() or None

    try:
        confirmed_med = PrescriptionMedicine.objects.create(
            prescription=prescription,
            medicine_name=medicine_name,
            strength=strength,
            dosage=dosage,
            frequency=frequency,
            duration=duration,
            instructions=instructions,
            source='AI_CONFIRMED',
            extracted_index=extracted_index,
        )
    except IntegrityError:
        err_msg = "This extracted medicine has already been confirmed."
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.warning(request, err_msg)
        return redirect('patient:prescription_detail', pk=prescription.pk)

    success_msg = f'Medicine "{confirmed_med.medicine_name}" confirmed successfully.'
    if is_ajax:
        summary = enrich_prescription_details(prescription)
        return JsonResponse({
            'status': 'success',
            'message': success_msg,
            'medicine': {
                'id': confirmed_med.id,
                'medicine_name': confirmed_med.medicine_name,
                'strength': confirmed_med.strength or '-',
                'dosage': confirmed_med.dosage or '-',
                'frequency': confirmed_med.frequency or '-',
                'duration': confirmed_med.duration or '-',
                'instructions': confirmed_med.instructions or '-',
                'source': confirmed_med.source,
                'extracted_index': confirmed_med.extracted_index,
                'created_at': confirmed_med.created_at.strftime('%b %d, %Y') if confirmed_med.created_at else '',
            },
            'summary': {
                'total_ai_count': summary['total_ai_count'],
                'confirmed_ai_count': summary['confirmed_ai_count'],
                'confirmation_summary': summary['confirmation_summary'],
                'confirmation_status': summary['confirmation_status'],
                'unconfirmed_count': len(summary['unconfirmed_extracted']),
            }
        })

    messages.success(request, success_msg)
    next_url = request.POST.get('next')
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    return redirect('patient:prescription_detail', pk=prescription.pk)


@role_required('PATIENT')
@require_POST
def delete_prescription_medicine_view(request, pk, med_id):
    """
    Allows patient to remove a confirmed or manually added medicine from their prescription.
    Scoped strictly to prescription owner.
    Supports both AJAX and standard POST redirect.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)
    medicine = get_object_or_404(PrescriptionMedicine, pk=med_id, prescription=prescription)
    med_name = medicine.medicine_name
    medicine.delete()

    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        'application/json' in request.headers.get('accept', '')
    )

    success_msg = f'Medicine "{med_name}" removed successfully.'
    if is_ajax:
        summary = enrich_prescription_details(prescription)
        return JsonResponse({
            'status': 'success',
            'message': success_msg,
            'summary': {
                'total_ai_count': summary['total_ai_count'],
                'confirmed_ai_count': summary['confirmed_ai_count'],
                'confirmation_summary': summary['confirmation_summary'],
                'confirmation_status': summary['confirmation_status'],
                'unconfirmed_count': len(summary['unconfirmed_extracted']),
            }
        })

    messages.success(request, success_msg)
    next_url = request.POST.get('next')
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    return redirect('patient:prescription_detail', pk=prescription.pk)



# Default search radius for nearby pharmacies in kilometers
DEFAULT_SEARCH_RADIUS_KM = 10.0


def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance between two geographic coordinates
    using the standard Haversine formula.
    Returns distance in kilometers rounded to 2 decimal places.
    """
    R = 6371.0  # Mean radius of Earth in km

    d_lat = math.radians(float(lat2) - float(lat1))
    d_lon = math.radians(float(lon2) - float(lon1))
    a = (math.sin(d_lat / 2.0) ** 2 +
         math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) *
         math.sin(d_lon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 2)


# Public Overpass API mirror endpoints for high availability
OVERPASS_ENDPOINTS = [
    'https://overpass-api.de/api/interpreter',
    'https://lz4.overpass-api.de/api/interpreter',
    'https://z.overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.private.coffee/api/interpreter',
    'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
]

# Official OpenStreetMap Nominatim search endpoint for rapid fallback
NOMINATIM_OSM_ENDPOINT = 'https://nominatim.openstreetmap.org/search'


class OsmResultList(list):
    """
    Standard Python list of OSM pharmacy dictionaries that carries an optional
    osm_error attribute indicating whether external OSM services were unreachable.
    """
    def __init__(self, items=(), osm_error=None):
        super().__init__(items)
        self.osm_error = osm_error


def fetch_osm_pharmacies(lat, lon, radius_km=DEFAULT_SEARCH_RADIUS_KM):
    """
    Query OpenStreetMap for public pharmacies within radius_km around (lat, lon).
    1. Checks local cache first (10 min TTL).
    2. Queries Overpass API mirror pool using raw Overpass QL payload.
    3. Fails over seamlessly to OpenStreetMap Nominatim API if Overpass mirrors fail or time out.
    4. Caches successful results for 10 minutes.
    5. Returns an OsmResultList instance with .osm_error attribute.
    """
    cache_key = f"osm_pharma_{round(float(lat), 3)}_{round(float(lon), 3)}_{int(radius_km)}"
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        return OsmResultList(cached_data)

    radius_meters = int(radius_km * 1000)
    overpass_query = f"""[out:json][timeout:15];(node["amenity"="pharmacy"](around:{radius_meters},{lat},{lon});way["amenity"="pharmacy"](around:{radius_meters},{lat},{lon}););out center;"""

    public_pharmacies = []
    # Send raw Overpass QL query in POST body (standard Overpass interpreter protocol)
    data = overpass_query.strip().encode('utf-8')
    headers = {
        'User-Agent': 'MedAccess-Healthcare-Platform/1.0 (contact: admin@medaccess.org)',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
    }

    # 1. Attempt Overpass mirrors
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode('utf-8'))
                    elements = payload.get('elements', [])
                    for el in elements:
                        p_lat = el.get('lat') or el.get('center', {}).get('lat')
                        p_lon = el.get('lon') or el.get('center', {}).get('lon')
                        if p_lat is None or p_lon is None:
                            continue

                        tags = el.get('tags', {})
                        name = tags.get('name') or tags.get('name:en') or tags.get('operator') or tags.get('brand') or 'Public Pharmacy'

                        addr_parts = []
                        if tags.get('addr:street'):
                            num = tags.get('addr:housenumber', '')
                            street = tags.get('addr:street', '')
                            addr_parts.append(f"{num} {street}".strip())
                        if tags.get('addr:suburb'):
                            addr_parts.append(tags.get('addr:suburb'))
                        elif tags.get('addr:district'):
                            addr_parts.append(tags.get('addr:district'))
                        if tags.get('addr:city'):
                            addr_parts.append(tags.get('addr:city'))
                        if tags.get('addr:postcode'):
                            addr_parts.append(tags.get('addr:postcode'))
                        address = ', '.join(addr_parts) if addr_parts else 'Address not listed in OpenStreetMap'

                        phone = tags.get('phone') or tags.get('contact:phone') or ''
                        dist = calculate_haversine_distance(lat, lon, p_lat, p_lon)

                        if dist <= radius_km:
                            public_pharmacies.append({
                                'id': f"osm_{el.get('id')}",
                                'osm_type': el.get('type', 'node'),
                                'osm_id': el.get('id'),
                                'name': name,
                                'source': 'OPENSTREETMAP',
                                'is_verified': False,
                                'badge_label': 'OpenStreetMap Listing',
                                'address': address,
                                'phone_number': phone,
                                'latitude': float(p_lat),
                                'longitude': float(p_lon),
                                'distance_km': dist,
                            })

                    # Cache successful query response for 10 minutes (600s)
                    if public_pharmacies:
                        cache.set(cache_key, public_pharmacies, 600)
                    return OsmResultList(public_pharmacies)
        except Exception as exc:
            logger.warning("Overpass endpoint %s failed: %s", endpoint, exc)
            continue

    # 2. Overpass mirrors unavailable: Fallback to official OpenStreetMap Nominatim Search API
    logger.info("Overpass mirrors busy or timed out; querying OpenStreetMap Nominatim fallback...")
    try:
        d_deg = float(radius_km) / 111.0
        left = float(lon) - d_deg
        right = float(lon) + d_deg
        top = float(lat) + d_deg
        bottom = float(lat) - d_deg
        nom_params = urllib.parse.urlencode({
            'amenity': 'pharmacy',
            'format': 'json',
            'addressdetails': '1',
            'extratags': '1',
            'viewbox': f"{left:.4f},{top:.4f},{right:.4f},{bottom:.4f}",
            'bounded': '1',
            'limit': '50',
        })
        nom_url = f"{NOMINATIM_OSM_ENDPOINT}?{nom_params}"
        nom_req = urllib.request.Request(nom_url, headers={
            'User-Agent': 'MedAccess-Healthcare-Platform/1.0 (contact: admin@medaccess.org)',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(nom_req, timeout=10) as nom_resp:
            if nom_resp.status == 200:
                nom_data = json.loads(nom_resp.read().decode('utf-8'))
                for el in nom_data:
                    p_lat = el.get('lat')
                    p_lon = el.get('lon')
                    if not p_lat or not p_lon:
                        continue
                    try:
                        p_lat_f = float(p_lat)
                        p_lon_f = float(p_lon)
                    except (ValueError, TypeError):
                        continue

                    dist = calculate_haversine_distance(lat, lon, p_lat_f, p_lon_f)
                    if dist <= radius_km:
                        name = el.get('name') or el.get('address', {}).get('amenity') or 'Public Pharmacy'
                        addr = el.get('display_name') or 'Address not listed in OpenStreetMap'
                        extratags = el.get('extratags') or {}
                        phone = extratags.get('phone') or extratags.get('contact:phone') or ''
                        public_pharmacies.append({
                            'id': f"osm_{el.get('osm_id')}",
                            'osm_type': el.get('osm_type', 'node'),
                            'osm_id': el.get('osm_id'),
                            'name': name,
                            'source': 'OPENSTREETMAP',
                            'is_verified': False,
                            'badge_label': 'OpenStreetMap Listing',
                            'address': addr,
                            'phone_number': phone,
                            'latitude': p_lat_f,
                            'longitude': p_lon_f,
                            'distance_km': dist,
                        })

                if public_pharmacies:
                    cache.set(cache_key, public_pharmacies, 600)
                return OsmResultList(public_pharmacies)
    except Exception as nom_exc:
        logger.warning("Nominatim OSM fallback query failed: %s", nom_exc)

    return OsmResultList([], osm_error="OpenStreetMap public pharmacy directory is temporarily unreachable. Please try again in a few moments.")


@role_required('PATIENT')
def nearby_pharmacies_view(request):
    """
    Renders the Nearby Pharmacies interactive Leaflet map interface.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)

    context = {
        'patient_profile': patient_profile,
        'default_radius_km': DEFAULT_SEARCH_RADIUS_KM,
    }
    return render(request, 'patient/nearby_pharmacies.html', context)


@role_required('PATIENT')
def nearby_pharmacies_api(request):
    """
    JSON API endpoint returning nearby pharmacies (MedAccess approved + OpenStreetMap)
    sorted nearest-first.
    """
    raw_lat = request.GET.get('lat')
    raw_lon = request.GET.get('lon')
    raw_radius = request.GET.get('radius', DEFAULT_SEARCH_RADIUS_KM)

    if not raw_lat or not raw_lon:
        return JsonResponse({
            'status': 'error',
            'message': 'Latitude and longitude parameters are required.'
        }, status=400)

    try:
        patient_lat = float(raw_lat)
        patient_lon = float(raw_lon)
        radius_km = float(raw_radius)
    except (ValueError, TypeError):
        return JsonResponse({
            'status': 'error',
            'message': 'Invalid latitude, longitude, or radius values.'
        }, status=400)

    if not (-90.0 <= patient_lat <= 90.0) or not (-180.0 <= patient_lon <= 180.0):
        return JsonResponse({
            'status': 'error',
            'message': 'Coordinates are out of valid geographic range.'
        }, status=400)

    # 1. MedAccess Verified Pharmacies (Only approved, with valid coordinates)
    patient_profile = getattr(request.user, 'patient_profile', None)
    saved_med_map = {}
    saved_osm_map = {}
    if patient_profile:
        for sp in patient_profile.saved_pharmacies.all():
            if sp.pharmacy_id:
                saved_med_map[sp.pharmacy_id] = sp.id
            elif sp.osm_id:
                saved_osm_map[(sp.osm_type, sp.osm_id)] = sp.id

    medaccess_results = []
    approved_profiles = PharmacyProfile.objects.filter(
        approval_status='APPROVED',
        latitude__isnull=False,
        longitude__isnull=False
    )

    for pharma in approved_profiles:
        try:
            p_lat = float(pharma.latitude)
            p_lon = float(pharma.longitude)
        except (ValueError, TypeError):
            continue

        dist = calculate_haversine_distance(patient_lat, patient_lon, p_lat, p_lon)
        if dist <= radius_km:
            full_addr = f"{pharma.address}, {pharma.city}, {pharma.state} {pharma.pincode}".strip(', ')
            medaccess_results.append({
                'id': pharma.id,
                'name': pharma.pharmacy_name,
                'source': 'MEDACCESS',
                'is_verified': True,
                'badge_label': 'MedAccess Verified',
                'is_saved': pharma.id in saved_med_map,
                'saved_id': saved_med_map.get(pharma.id),
                'address': full_addr,
                'phone_number': pharma.phone_number or '',
                'latitude': p_lat,
                'longitude': p_lon,
                'distance_km': dist,
            })

    # 2. OpenStreetMap Public Pharmacies
    osm_results = fetch_osm_pharmacies(patient_lat, patient_lon, radius_km)
    osm_error = getattr(osm_results, 'osm_error', None)
    for osm_p in osm_results:
        osm_key = (osm_p.get('osm_type', 'node'), osm_p.get('osm_id'))
        osm_p['is_saved'] = osm_key in saved_osm_map
        osm_p['saved_id'] = saved_osm_map.get(osm_key)

    # 3. Combine and sort nearest first
    combined = medaccess_results + osm_results
    combined.sort(key=lambda x: x['distance_km'])

    # Determine status & failure distinction
    status = 'success'
    osm_unavailable = bool(osm_error and not osm_results)
    response_payload = {
        'status': status,
        'patient_lat': patient_lat,
        'patient_lon': patient_lon,
        'radius_km': radius_km,
        'total_count': len(combined),
        'medaccess_count': len(medaccess_results),
        'public_count': len(osm_results),
        'pharmacies': combined,
        'osm_unavailable': osm_unavailable,
    }

    if osm_unavailable:
        response_payload['osm_warning'] = osm_error
        # If no pharmacies were found anywhere because OSM was down and no local DB matches exist
        if len(combined) == 0:
            response_payload['status'] = 'warning'
            response_payload['message'] = osm_error

    return JsonResponse(response_payload)


@role_required('PATIENT')
def saved_pharmacies_list_view(request):
    """
    Displays all pharmacies saved/bookmarked by the authenticated patient.
    Supports both MedAccess verified pharmacies and OpenStreetMap public listings.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    saved_pharmacies = []
    if patient_profile:
        saved_pharmacies = patient_profile.saved_pharmacies.select_related('pharmacy').order_by('-saved_at')

    context = {
        'patient_profile': patient_profile,
        'saved_pharmacies': saved_pharmacies,
    }
    return render(request, 'patient/saved_pharmacies.html', context)


@role_required('PATIENT')
@require_POST
def save_pharmacy_view(request):
    """
    Saves either an approved MedAccess pharmacy or a public OpenStreetMap pharmacy.
    Prevents duplicate saves. Never trusts patient IDs from client.
    """
    patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    # Extract payload (support both JSON body and standard form-encoded POST)
    payload = {}
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    else:
        payload = request.POST

    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.content_type == 'application/json'
    )

    source = str(payload.get('source', '')).upper()
    pharmacy_id = payload.get('pharmacy_id')
    osm_id = payload.get('osm_id')
    osm_type = str(payload.get('osm_type', 'node')).lower()

    # Case A: OpenStreetMap Public Pharmacy
    if osm_id or source == 'OPENSTREETMAP':
        if osm_type not in ('node', 'way', 'relation'):
            return JsonResponse({'status': 'error', 'message': 'Invalid OpenStreetMap element type.'}, status=400)

        try:
            valid_osm_id = int(osm_id)
            if valid_osm_id <= 0:
                raise ValueError("osm_id must be positive")
        except (ValueError, TypeError):
            return JsonResponse({'status': 'error', 'message': 'Invalid OpenStreetMap ID format.'}, status=400)

        # Validate coordinates
        raw_lat = payload.get('latitude')
        raw_lon = payload.get('longitude')
        lat_dec = None
        lon_dec = None
        if raw_lat is not None and raw_lon is not None:
            try:
                f_lat = float(raw_lat)
                f_lon = float(raw_lon)
                if not (-90.0 <= f_lat <= 90.0) or not (-180.0 <= f_lon <= 180.0):
                    raise ValueError("Coordinates out of valid range")
                lat_dec = Decimal(str(round(f_lat, 7)))
                lon_dec = Decimal(str(round(f_lon, 7)))
            except (ValueError, TypeError, InvalidOperation):
                return JsonResponse({'status': 'error', 'message': 'Coordinates are out of valid range.'}, status=400)

        pharmacy_name = str(payload.get('pharmacy_name') or payload.get('name') or 'Public Pharmacy').strip()[:255]
        address = str(payload.get('address') or 'Address not listed in OpenStreetMap').strip()
        phone_number = str(payload.get('phone_number') or '').strip()[:50]

        # Prevent duplicate OSM saves for this patient
        saved_record, created = SavedPharmacy.objects.get_or_create(
            patient=patient_profile,
            osm_type=osm_type,
            osm_id=valid_osm_id,
            defaults={
                'pharmacy_name': pharmacy_name,
                'address': address,
                'latitude': lat_dec,
                'longitude': lon_dec,
                'phone_number': phone_number,
            }
        )

        if not created:
            msg = f"{saved_record.display_name} is already in your saved list."
            if is_ajax:
                return JsonResponse({'status': 'info', 'message': msg, 'already_saved': True, 'saved_id': saved_record.id})
            messages.info(request, msg)
            return redirect('patient:saved_pharmacies')

        msg = f"{saved_record.display_name} has been added to your saved pharmacies."
        if is_ajax:
            return JsonResponse({'status': 'success', 'message': msg, 'already_saved': False, 'saved_id': saved_record.id})
        messages.success(request, msg)
        return redirect('patient:saved_pharmacies')

    # Case B: MedAccess Registered Pharmacy
    if not pharmacy_id:
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': 'Pharmacy ID or OSM ID is required.'}, status=400)
        messages.error(request, 'Pharmacy ID or OSM ID is required.')
        return redirect('patient:nearby_pharmacies')

    try:
        valid_pharmacy_id = int(pharmacy_id)
    except (ValueError, TypeError):
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': 'Invalid pharmacy ID format.'}, status=400)
        messages.error(request, 'Invalid pharmacy ID format.')
        return redirect('patient:nearby_pharmacies')

    # Security: Ensure pharmacy exists and has approval_status='APPROVED'
    pharmacy = PharmacyProfile.objects.filter(id=valid_pharmacy_id, approval_status='APPROVED').first()
    if not pharmacy:
        if is_ajax:
            return JsonResponse({
                'status': 'error',
                'message': 'Pharmacy not found or not approved for saving.'
            }, status=404)
        messages.error(request, 'Pharmacy not found or not approved for saving.')
        return redirect('patient:nearby_pharmacies')

    # Prevent duplicate MedAccess saves
    saved_record, created = SavedPharmacy.objects.get_or_create(
        patient=patient_profile,
        pharmacy=pharmacy
    )

    if not created:
        msg = f"{pharmacy.pharmacy_name} is already in your saved list."
        if is_ajax:
            return JsonResponse({'status': 'info', 'message': msg, 'already_saved': True, 'saved_id': saved_record.id})
        messages.info(request, msg)
        return redirect('patient:saved_pharmacies')

    msg = f"{pharmacy.pharmacy_name} has been added to your saved pharmacies."
    if is_ajax:
        return JsonResponse({'status': 'success', 'message': msg, 'already_saved': False, 'saved_id': saved_record.id})

    messages.success(request, msg)
    return redirect('patient:saved_pharmacies')


@role_required('PATIENT')
@require_POST
def unsave_pharmacy_view(request):
    """
    Toggles/un-saves a pharmacy by either saved_id, pharmacy_id, or osm_type+osm_id.
    Enforces that patients can only un-save their own records.
    """
    patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    payload = {}
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    else:
        payload = request.POST

    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.content_type == 'application/json'
    )

    saved_id = payload.get('saved_id')
    pharmacy_id = payload.get('pharmacy_id')
    osm_id = payload.get('osm_id')
    osm_type = str(payload.get('osm_type', 'node')).lower()

    saved_record = None
    if saved_id:
        try:
            saved_record = SavedPharmacy.objects.filter(pk=int(saved_id), patient=patient_profile).first()
        except (ValueError, TypeError):
            pass
    elif pharmacy_id:
        try:
            saved_record = SavedPharmacy.objects.filter(pharmacy_id=int(pharmacy_id), patient=patient_profile).first()
        except (ValueError, TypeError):
            pass
    elif osm_id:
        try:
            saved_record = SavedPharmacy.objects.filter(osm_type=osm_type, osm_id=int(osm_id), patient=patient_profile).first()
        except (ValueError, TypeError):
            pass

    if not saved_record:
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': 'Saved pharmacy record not found.'}, status=404)
        messages.error(request, 'Saved pharmacy record not found.')
        return redirect('patient:saved_pharmacies')

    pharmacy_name = saved_record.display_name
    saved_record.delete()

    msg = f"{pharmacy_name} has been removed from your saved pharmacies."
    if is_ajax:
        return JsonResponse({'status': 'success', 'message': msg})

    messages.success(request, msg)
    return redirect('patient:saved_pharmacies')


@role_required('PATIENT')
@require_POST
def remove_saved_pharmacy_view(request, pk):
    """
    Removes a saved pharmacy record by primary key.
    Security: Patient can only remove records belonging to their own profile.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    saved_record = get_object_or_404(SavedPharmacy, pk=pk, patient=patient_profile)
    pharmacy_name = saved_record.display_name
    saved_record.delete()

    msg = f"{pharmacy_name} has been removed from your saved pharmacies."
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'status': 'success', 'message': msg})

    messages.success(request, msg)
    return redirect('patient:saved_pharmacies')


@role_required('PATIENT')
def feedback_view(request):
    """
    Patient Portal Feedback & Ratings page.
    Allows patients to search approved pharmacies, submit 1-5 star ratings with
    mandatory comments, edit existing feedback, and view their submitted reviews.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile and getattr(request.user, 'role', '') == 'PATIENT':
        patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    # Security: Query strictly approved pharmacies whose accounts are active
    approved_pharmacies_qs = PharmacyProfile.objects.filter(
        approval_status='APPROVED',
        user__is_active=True
    )

    # Search filter by name, city, address
    search_query = request.GET.get('q', '').strip()
    if search_query:
        pharmacies = approved_pharmacies_qs.filter(
            Q(pharmacy_name__icontains=search_query) |
            Q(city__icontains=search_query) |
            Q(address__icontains=search_query)
        ).order_by('pharmacy_name')
    else:
        pharmacies = approved_pharmacies_qs.order_by('pharmacy_name')

    # Handle selected pharmacy
    selected_pharmacy_id = request.POST.get('pharmacy_id') or request.GET.get('pharmacy_id')
    selected_pharmacy = None
    if selected_pharmacy_id:
        try:
            valid_id = int(selected_pharmacy_id)
            selected_pharmacy = approved_pharmacies_qs.filter(id=valid_id).first()
        except (ValueError, TypeError):
            selected_pharmacy = None

    # Check for existing feedback for this patient and selected pharmacy
    existing_feedback = None
    if selected_pharmacy and patient_profile:
        existing_feedback = Feedback.objects.filter(
            patient=patient_profile,
            pharmacy=selected_pharmacy
        ).first()

    # Form handling
    if request.method == 'POST':
        # Reject if no valid approved pharmacy is selected
        if not selected_pharmacy:
            messages.error(request, 'Please select a valid approved pharmacy to review.')
            form = PatientFeedbackForm(request.POST)
            submitted_feedbacks = Feedback.objects.filter(
                patient=patient_profile
            ).select_related('pharmacy').order_by('-created_at')
            reviewed_pharmacy_ids = set(submitted_feedbacks.values_list('pharmacy_id', flat=True))
            context = {
                'pharmacies': pharmacies,
                'search_query': search_query,
                'selected_pharmacy': None,
                'existing_feedback': None,
                'form': form,
                'submitted_feedbacks': submitted_feedbacks,
                'reviewed_pharmacy_ids': reviewed_pharmacy_ids,
            }
            return render(request, 'patient/feedback.html', context, status=400)

        form = PatientFeedbackForm(request.POST, instance=existing_feedback)
        if form.is_valid():
            rating = form.cleaned_data['rating']
            comment = form.cleaned_data['comment']

            if existing_feedback:
                existing_feedback.rating = rating
                existing_feedback.comment = comment
                existing_feedback.save()
                messages.success(request, f'Your feedback for {selected_pharmacy.pharmacy_name} has been updated successfully.')
            else:
                Feedback.objects.create(
                    patient=patient_profile,
                    pharmacy=selected_pharmacy,
                    rating=rating,
                    comment=comment
                )
                messages.success(request, f'Thank you! Your feedback for {selected_pharmacy.pharmacy_name} has been submitted successfully.')

            return redirect('patient:feedback')
        else:
            messages.error(request, 'Please provide both a valid star rating (1-5) and a written comment.')
            submitted_feedbacks = Feedback.objects.filter(
                patient=patient_profile
            ).select_related('pharmacy').order_by('-created_at')
            reviewed_pharmacy_ids = set(submitted_feedbacks.values_list('pharmacy_id', flat=True))
            context = {
                'pharmacies': pharmacies,
                'search_query': search_query,
                'selected_pharmacy': selected_pharmacy,
                'existing_feedback': existing_feedback,
                'form': form,
                'submitted_feedbacks': submitted_feedbacks,
                'reviewed_pharmacy_ids': reviewed_pharmacy_ids,
            }
            return render(request, 'patient/feedback.html', context, status=400)
    else:
        if existing_feedback:
            form = PatientFeedbackForm(instance=existing_feedback)
        else:
            form = PatientFeedbackForm()

    # Patient's own submitted feedbacks
    submitted_feedbacks = Feedback.objects.filter(
        patient=patient_profile
    ).select_related('pharmacy').order_by('-created_at')
    reviewed_pharmacy_ids = set(submitted_feedbacks.values_list('pharmacy_id', flat=True))

    context = {
        'pharmacies': pharmacies,
        'search_query': search_query,
        'selected_pharmacy': selected_pharmacy,
        'existing_feedback': existing_feedback,
        'form': form,
        'submitted_feedbacks': submitted_feedbacks,
        'reviewed_pharmacy_ids': reviewed_pharmacy_ids,
    }
    return render(request, 'patient/feedback.html', context)


# ============================================================================
# Phase 2: Prescription Request System Views
# ============================================================================

@role_required('PATIENT')
def check_pharmacies_availability_view(request, pk):
    """
    Evaluates confirmed prescription medicines against nearby approved pharmacies.
    GET /patient/prescriptions/<int:pk>/check-pharmacies/
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        return JsonResponse({'status': 'error', 'message': 'Patient profile not found.'}, status=403)

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)

    if prescription.status != 'COMPLETED':
        return JsonResponse({
            'status': 'error',
            'message': 'Availability can only be evaluated for completed prescriptions.'
        }, status=400)

    if not prescription.medicines.exists():
        return JsonResponse({
            'status': 'error',
            'message': 'No confirmed medicines found on this prescription.'
        }, status=400)

    # Resolve latitude, longitude, and radius
    lat = request.GET.get('lat')
    lon = request.GET.get('lon')
    radius = request.GET.get('radius')

    if lat is not None and lon is not None and str(lat).strip() != '' and str(lon).strip() != '':
        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            return JsonResponse({'status': 'error', 'message': 'Invalid coordinates provided.'}, status=400)
    else:
        # Fall back to patient_profile saved coordinates
        if patient_profile.current_latitude is not None and patient_profile.current_longitude is not None:
            lat = float(patient_profile.current_latitude)
            lon = float(patient_profile.current_longitude)
        else:
            return JsonResponse({
                'status': 'location_required',
                'message': 'Patient location is required to search for nearby pharmacies. Please enable location services or update your location in profile.'
            }, status=400)

    # Validate coordinate ranges
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return JsonResponse({'status': 'error', 'message': 'Coordinates are out of geographic range.'}, status=400)

    try:
        radius_km = float(radius) if radius else 10.0
        radius_km = max(0.5, min(radius_km, 50.0))
    except (ValueError, TypeError):
        radius_km = 10.0

    reports = check_prescription_pharmacy_availability(
        prescription=prescription,
        patient_lat=lat,
        patient_lon=lon,
        radius_km=radius_km
    )

    # Identify pharmacies with existing active requests for this prescription
    active_pharmacy_ids = set(
        PrescriptionRequest.objects.filter(
            prescription=prescription,
            status__in=PrescriptionRequest.ACTIVE_STATUSES
        ).values_list('pharmacy_id', flat=True)
    )

    pharmacies_data = []
    for rep in reports:
        rep_dict = rep.as_dict()
        rep_dict['has_active_request'] = rep.pharmacy_id in active_pharmacy_ids
        pharmacies_data.append(rep_dict)

    return JsonResponse({
        'status': 'success',
        'count': len(pharmacies_data),
        'radius_km': radius_km,
        'patient_lat': lat,
        'patient_lon': lon,
        'pharmacies': pharmacies_data
    })


@role_required('PATIENT')
@require_POST
def send_prescription_request_view(request, pk):
    """
    Creates a new fulfillment request from a patient prescription to a selected approved pharmacy.
    POST /patient/prescriptions/<int:pk>/send-request/
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': 'Patient profile not found.'}, status=403)
        messages.error(request, 'Patient profile not found.')
        return redirect('patient:prescriptions_list')

    prescription = get_object_or_404(Prescription, pk=pk, patient=patient_profile)

    if prescription.status != 'COMPLETED':
        msg = 'Requests can only be submitted for completed prescriptions.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    if not prescription.medicines.exists():
        msg = 'No confirmed medicines found on this prescription.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    # Pharmacy selection validation
    pharmacy_id = request.POST.get('pharmacy_id')
    if not pharmacy_id:
        msg = 'Please select a pharmacy for fulfillment.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    try:
        pharmacy = PharmacyProfile.objects.select_related('user').get(pk=pharmacy_id, approval_status='APPROVED')
    except PharmacyProfile.DoesNotExist:
        msg = 'The selected pharmacy is either invalid or not approved.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    # Explicit sharing consent validation
    consent = request.POST.get('consent')
    is_consented = str(consent).lower() in ['1', 'true', 'yes', 'on']
    if not is_consented:
        msg = 'Explicit consent is required to share prescription and contact details with the pharmacy.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    patient_notes = (request.POST.get('patient_notes') or '').strip() or None

    try:
        with transaction.atomic():
            # Lock the prescription row to avoid race conditions
            locked_rx = Prescription.objects.select_for_update().get(pk=prescription.pk, patient=patient_profile)

            # Check for existing active requests with this specific pharmacy
            existing_active = PrescriptionRequest.objects.filter(
                prescription=locked_rx,
                pharmacy=pharmacy,
                status__in=PrescriptionRequest.ACTIVE_STATUSES
            ).first()

            if existing_active:
                msg = (
                    f"An active request (#{existing_active.id}) already exists for this prescription "
                    f"with {pharmacy.pharmacy_name} (Status: {existing_active.get_status_display()})."
                )
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': 'duplicate_request',
                        'message': msg,
                        'request_id': existing_active.id,
                        'redirect_url': f'/patient/requests/{existing_active.id}/'
                    }, status=409)
                messages.warning(request, msg)
                return redirect('patient:request_detail', pk=existing_active.id)

            # Recalculate availability server-side; NEVER trust frontend values
            if pharmacy.latitude is not None and pharmacy.longitude is not None:
                reports = check_prescription_pharmacy_availability(
                    prescription=locked_rx,
                    patient_lat=float(pharmacy.latitude),
                    patient_lon=float(pharmacy.longitude),
                    radius_km=1.0
                )
                pharm_report = next((r for r in reports if r.pharmacy_id == pharmacy.id), None)
            else:
                pharm_report = None

            if pharm_report:
                initial_avail = pharm_report.overall_status
                item_map = {
                    it.prescription_medicine_id: it
                    for it in pharm_report.items
                    if it.prescription_medicine_id is not None
                }
            else:
                initial_avail = 'PARTIALLY_AVAILABLE'
                item_map = {}

            # Create PrescriptionRequest
            presc_request = PrescriptionRequest.objects.create(
                patient=patient_profile,
                pharmacy=pharmacy,
                prescription=locked_rx,
                status='PENDING',
                initial_availability_status=initial_avail,
                patient_notes=patient_notes,
            )

            # Create immutable snapshot line items
            items_to_create = []
            for med in locked_rx.medicines.all().order_by('id'):
                av_item = item_map.get(med.id)
                items_to_create.append(PrescriptionRequestItem(
                    request=presc_request,
                    prescription_medicine=med,
                    matched_catalogue_medicine_id=av_item.matched_catalogue_id if av_item else None,
                    prescribed_name_snapshot=med.medicine_name,
                    prescribed_strength_snapshot=med.strength or '',
                    prescribed_dosage_snapshot=med.dosage or '',
                    prescribed_frequency_snapshot=med.frequency or '',
                    prescribed_duration_snapshot=med.duration or '',
                    prescribed_quantity_snapshot=med.quantity or '',
                    prescribed_instructions_snapshot=med.instructions or '',
                    match_status=av_item.match_status if av_item else 'UNMAPPED',
                    availability_status=av_item.availability_status if av_item else 'NOT_STOCKED',
                    required_quantity=av_item.required_quantity if av_item else None,
                    required_quantity_note=av_item.required_quantity_note if av_item else '',
                    available_quantity_snapshot=av_item.available_quantity if av_item else 0,
                    explanation=av_item.explanation if av_item else 'Availability not evaluated.',
                ))

            PrescriptionRequestItem.objects.bulk_create(items_to_create)

            # Notification 1 — Pharmacy alert
            patient_display = patient_profile.user.get_full_name() or patient_profile.user.username
            create_request_notification(
                request=presc_request,
                recipient=pharmacy.user,
                notification_type='REQUEST_SUBMITTED',
                title='New Prescription Request',
                message=(
                    f"New prescription request #{presc_request.id} received from {patient_display}. "
                    "Available for review in your Prescription Requests inbox."
                ),
                actor=patient_profile.user
            )

            # Notification 2 — Patient confirmation
            create_request_notification(
                request=presc_request,
                recipient=patient_profile.user,
                notification_type='REQUEST_SUBMITTED',
                title='Request Submitted Successfully',
                message=(
                    f"Your prescription request #{presc_request.id} has been submitted to {pharmacy.pharmacy_name}."
                ),
                actor=patient_profile.user
            )

    except IntegrityError as e:
        logger.exception("IntegrityError while creating prescription request: %s", e)
        msg = "A database integrity error occurred while saving your request. Please try again."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': msg}, status=500)
        messages.error(request, msg)
        return redirect('patient:prescription_detail', pk=pk)

    success_msg = f"Prescription request #{presc_request.id} sent successfully to {pharmacy.pharmacy_name}."
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'success',
            'message': success_msg,
            'request_id': presc_request.id,
            'redirect_url': f'/patient/requests/{presc_request.id}/'
        })

    messages.success(request, success_msg)
    return redirect('patient:request_detail', pk=presc_request.id)


@role_required('PATIENT')
@require_POST
def cancel_prescription_request_view(request, pk):
    """
    Allows a patient to cancel a request that is still in PENDING status.
    POST /patient/requests/<int:pk>/cancel/
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    with transaction.atomic():
        presc_request = get_object_or_404(
            PrescriptionRequest.objects.select_for_update().select_related('patient__user', 'pharmacy__user'),
            pk=pk,
            patient=patient_profile
        )

        if presc_request.status != 'PENDING':
            messages.error(request, f"Request #{presc_request.id} cannot be cancelled because it is {presc_request.get_status_display()}.")
            return redirect('patient:request_detail', pk=pk)

        presc_request.status = 'CANCELLED'
        presc_request.save(update_fields=['status', 'updated_at'])

        patient_display = patient_profile.user.get_full_name() or patient_profile.user.username

        # Notification 1 — Pharmacy alert
        create_request_notification(
            request=presc_request,
            recipient=presc_request.pharmacy.user,
            notification_type='REQUEST_CANCELLED',
            title='Prescription Request Cancelled',
            message=f"Prescription request #{presc_request.id} was cancelled by {patient_display}.",
            actor=patient_profile.user
        )

        # Notification 2 — Patient confirmation
        create_request_notification(
            request=presc_request,
            recipient=patient_profile.user,
            notification_type='REQUEST_CANCELLED',
            title='Request Cancelled',
            message=f"Prescription request #{presc_request.id} has been cancelled successfully.",
            actor=patient_profile.user
        )

    messages.success(request, f"Request #{presc_request.id} has been cancelled.")
    return redirect('patient:request_detail', pk=pk)


@role_required('PATIENT')
def request_list_view(request):
    """
    Patient tracking page: lists all prescription requests sent by the logged-in patient.
    GET /patient/requests/
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    requests_qs = (
        PrescriptionRequest.objects.filter(patient=patient_profile)
        .select_related('pharmacy', 'prescription')
        .prefetch_related('items')
        .order_by('-created_at')
    )

    # Compute status counts for tab badges
    total_count = requests_qs.count()
    pending_count = requests_qs.filter(status='PENDING').count()
    active_count = requests_qs.filter(status__in=['ACCEPTED', 'PROCESSING', 'READY_FOR_PICKUP']).count()
    completed_count = requests_qs.filter(status='COMPLETED').count()

    context = {
        'patient_profile': patient_profile,
        'prescription_requests': requests_qs,
        'total_count': total_count,
        'pending_count': pending_count,
        'active_count': active_count,
        'completed_count': completed_count,
    }
    return render(request, 'patient/request_list.html', context)


@role_required('PATIENT')
def request_detail_view(request, pk):
    """
    Detailed tracking view for a specific prescription request.
    GET /patient/requests/<int:pk>/
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile:
        raise Http404("Patient profile not found.")

    presc_request = get_object_or_404(
        PrescriptionRequest.objects.select_related('pharmacy', 'prescription', 'patient').prefetch_related('items'),
        pk=pk,
        patient=patient_profile
    )

    items = presc_request.items.all().order_by('id')

    context = {
        'patient_profile': patient_profile,
        'prescription_request': presc_request,
        'items': items,
    }
    return render(request, 'patient/request_detail.html', context)


@login_required
def prescription_secure_image_view(request, pk):
    """
    Secure authenticated access endpoint for prescription documents.
    GET /patient/prescriptions/<int:pk>/image/

    Access Rules:
    1. Patient owner: must own the prescription.
    2. Pharmacy: must have an active, accepted, or fulfilled request for this prescription.
    3. Administrator: staff or admin users.
    4. All other users: denied with HTTP 403.
    """
    prescription = get_object_or_404(Prescription, pk=pk)
    user = request.user
    authorized = False

    if hasattr(user, 'patient_profile') and prescription.patient_id == user.patient_profile.id:
        authorized = True
    elif hasattr(user, 'pharmacy_profile') and PrescriptionRequest.objects.filter(
        prescription=prescription,
        pharmacy=user.pharmacy_profile,
        status__in=['PENDING', 'ACCEPTED', 'PROCESSING', 'READY_FOR_PICKUP', 'COMPLETED']
    ).exists():
        authorized = True
    elif user.is_staff or user.is_superuser or getattr(user, 'role', '').upper() == 'ADMIN':
        authorized = True

    if not authorized:
        raise PermissionDenied("You do not have authorization to view this prescription document.")

    if not prescription.prescription_image:
        raise Http404("No document attached to this prescription.")

    try:
        storage = prescription.prescription_image.storage
        if not storage.exists(prescription.prescription_image.name):
            raise Http404("Prescription document does not exist in storage.")
        f = prescription.prescription_image.open('rb')
    except Http404:
        raise
    except Exception:
        raise Http404("Prescription document could not be opened.")

    content_type, _ = mimetypes.guess_type(prescription.prescription_image.name)
    response = FileResponse(f, content_type=content_type or 'application/octet-stream')
    response['Cache-Control'] = 'private, no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


