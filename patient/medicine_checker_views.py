"""
Medicine Checker Views for Patient Portal
Provides web interface and JSON API for patients to look up medicine
clinical uses, composition, and side effects.
"""

import logging
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from accounts.decorators import role_required
from patient.models import PatientProfile
from patient.services.medicine_checker_service import (
    check_medicine_uses_and_side_effects,
    get_medicine_by_exact_name,
    get_medicine_autocomplete_suggestions,
)

logger = logging.getLogger(__name__)


@role_required('PATIENT')
def medicine_checker_view(request):
    """
    Renders the Medicine Uses & Side Effects Checker page in the Patient Portal.
    Optionally performs an initial server-rendered search if query parameter 'q' is present.
    """
    patient_profile = getattr(request.user, 'patient_profile', None)
    if not patient_profile and getattr(request.user, 'role', '') == 'PATIENT':
        patient_profile, _ = PatientProfile.objects.get_or_create(user=request.user)

    query = request.GET.get('q', '').strip()
    confirmed_name = request.GET.get('confirmed_name', '').strip()
    initial_result = None

    if confirmed_name:
        rec = get_medicine_by_exact_name(confirmed_name)
        if rec:
            initial_result = {
                'success': True,
                'query': confirmed_name,
                'match_status': 'HIGH',
                'similarity_score': 1.0,
                'best_match': rec,
                'candidates': [rec],
                'is_confirmed': True,
                'disclaimer': (
                    "Clinical uses and side effects reference based on Medicine_Details dataset. "
                    "Similarity score reflects text alignment, not medical diagnostic probability. "
                    "Always consult a licensed healthcare professional before taking medication."
                )
            }
    elif query:
        initial_result = check_medicine_uses_and_side_effects(query, top_k=5)

    context = {
        'patient_profile': patient_profile,
        'initial_query': query or confirmed_name,
        'initial_result': initial_result,
    }
    return render(request, 'patient/medicine_checker.html', context)


@role_required('PATIENT')
@require_GET
def medicine_checker_api(request):
    """
    Dedicated JSON GET API endpoint for live medicine search and confirmation.
    Used by the client-side interactive Medicine Checker interface.
    """
    confirmed_name = request.GET.get('confirmed_name', '').strip()
    if confirmed_name:
        rec = get_medicine_by_exact_name(confirmed_name)
        if rec:
            return JsonResponse({
                'success': True,
                'query': confirmed_name,
                'match_status': 'HIGH',
                'similarity_score': 1.0,
                'best_match': rec,
                'candidates': [rec],
                'is_confirmed': True,
                'disclaimer': (
                    "Clinical uses and side effects reference based on Medicine_Details dataset. "
                    "Always consult a licensed healthcare professional before taking medication."
                )
            })
        else:
            return JsonResponse({
                'success': False,
                'query': confirmed_name,
                'match_status': 'NO_MATCH',
                'message': f"Medicine '{confirmed_name}' could not be located."
            }, status=404)

    query = request.GET.get('q', '').strip()
    result = check_medicine_uses_and_side_effects(query, top_k=5)
    return JsonResponse(result)


@role_required('PATIENT')
@require_GET
def medicine_checker_autocomplete_api(request):
    """
    Dedicated GET API endpoint for live medicine autocomplete suggestions.
    Returns up to 7 suggestions containing canonical medicine_name and composition.
    Strictly does not return uses or side effects.
    """
    query = (request.GET.get('q') or request.GET.get('term') or '').strip()
    suggestions = get_medicine_autocomplete_suggestions(query, limit=7)
    return JsonResponse({
        'status': 'success',
        'query': query,
        'count': len(suggestions),
        'suggestions': suggestions
    })


