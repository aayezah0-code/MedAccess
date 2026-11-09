"""
Public Allergy Checker Views for MedAccess.
Accessible to all visitors without requiring registration or portal login.
Provides both full-page server rendering and asynchronous JSON API for seamless screening.
"""

import logging
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST

from patient.services.allergy_checker_service import (
    analyze_medicine_allergy,
    AllergyCheckerValidationError,
    AllergyCheckerRateLimitError,
    AllergyCheckerTimeoutError,
    AllergyCheckerAPIError,
    AllergyCheckerModelError,
    AllergyCheckerAuthError,
)

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str:
    """Extracts client IP address respecting X-Forwarded-For if behind a proxy."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    return ip or '127.0.0.1'


@require_http_methods(["GET", "POST"])
def allergy_checker_view(request):
    """
    Public Allergy Checker page view.
    Handles GET (renders input form) and standard POST form submissions
    (progressive enhancement for users without JavaScript).
    """
    context = {
        'initial_medicine': '',
        'initial_symptoms': '',
        'result': None,
        'error_message': None,
    }

    if request.method == 'POST':
        medicine_name = request.POST.get('medicine_name', '').strip()
        symptoms = request.POST.get('symptoms', '').strip()
        image_file = request.FILES.get('medicine_image', None)
        client_ip = _get_client_ip(request)

        context['initial_medicine'] = medicine_name
        context['initial_symptoms'] = symptoms

        try:
            result = analyze_medicine_allergy(
                medicine_name=medicine_name,
                image_file=image_file,
                symptoms=symptoms,
                client_ip=client_ip,
            )
            context['result'] = result
        except AllergyCheckerValidationError as exc:
            context['error_message'] = str(exc)
        except AllergyCheckerRateLimitError as exc:
            context['error_message'] = str(exc)
        except (AllergyCheckerTimeoutError, AllergyCheckerAPIError) as exc:
            context['error_message'] = str(exc)
        except Exception as exc:
            logger.error("Unexpected error in allergy_checker_view: %s", type(exc).__name__)
            context['error_message'] = "An unexpected error occurred while processing your request. Please try again or consult a doctor."

    return render(request, 'patient/allergy_checker.html', context)


@require_POST
def allergy_checker_api(request):
    """
    Public JSON POST endpoint for live client-side allergy screening.
    Preserves CSRF protection and provides structured responses.
    """
    medicine_name = request.POST.get('medicine_name', '').strip()
    symptoms = request.POST.get('symptoms', '').strip()
    image_file = request.FILES.get('medicine_image', None)
    client_ip = _get_client_ip(request)

    try:
        result = analyze_medicine_allergy(
            medicine_name=medicine_name,
            image_file=image_file,
            symptoms=symptoms,
            client_ip=client_ip,
        )
        return JsonResponse({
            'success': True,
            'data': result,
        }, status=200)

    except AllergyCheckerValidationError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=400)

    except AllergyCheckerRateLimitError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=429)

    except AllergyCheckerTimeoutError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=504)

    except AllergyCheckerAuthError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=401)

    except AllergyCheckerModelError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=502)

    except AllergyCheckerAPIError as exc:
        return JsonResponse({
            'success': False,
            'error': str(exc),
        }, status=502)

    except Exception as exc:
        logger.error("Unexpected error in allergy_checker_api: %s", type(exc).__name__)
        return JsonResponse({
            'success': False,
            'error': "An internal error occurred while analyzing medication symptoms. Please try again or consult a doctor.",
        }, status=500)

