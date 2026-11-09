"""
MedAccess AI-Powered Medicine Allergy Checker Service.
Integrates Groq API with Llama 3.2 Vision / Llama 3.3 Text models to analyze
reported adverse drug reactions and clinical allergy symptoms.

Clinical Safety Directives:
- Never diagnoses an allergy or claims definitive medical certainty.
- Never guarantees medicine safety or absence of allergic risk.
- Never recommends re-dosing or self-medication.
- Immediately elevates red-flag symptoms (anaphylaxis, airway compromise) to emergency status.
- Strict zero-credential leakage and privacy-safe in-memory processing.
"""

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Max upload limit: 5 MB
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = {
    'image/jpeg': 'image/jpeg',
    'image/jpg': 'image/jpeg',
    'image/png': 'image/png',
    'image/webp': 'image/webp',
}

# Standard Medical Disclaimer
MANDATORY_MEDICAL_DISCLAIMER = (
    "This tool provides informational screening only. It cannot confirm or rule out a medicine allergy. "
    "Consult a qualified healthcare professional for medical advice. If you experience severe symptoms "
    "such as breathing difficulty or swelling of the throat or tongue, seek emergency medical assistance immediately."
)

# Red-flag emergency indicators across English, Hindi, and Hinglish
EMERGENCY_KEYWORDS = [
    # English
    r'\bbreath(ing)?\s+(trouble|difficult(y)?|problem|short(ness)?)\b',
    r'\b(shortness\s+of\s+breath|cannot\s+breathe|hard\s+to\s+breathe|gasping|wheez(e|ing))\b',
    r'\bthroat\s+(swell(ing)?|tight(ness)?|clos(ed|ing)|chok(e|ing))\b',
    r'\b(tongue|lip|face|facial)\s+swell(ing)?\b',
    r'\b(anaphylax(is|ic)|faint(ing|ed)?|pass(ed)?\s+out|unconscious(ness)?|collaps(e|ed)?)\b',
    r'\bchest\s+(pain|tight(ness)?|pressur(e)?)\b',
    r'\b(blue\s+(lips|skin|fingers)|cyanosis)\b',
    r'\brapid(ly)?\s+spread(ing)?\s+(rash|hives|welts)\b',
    # Hindi / Hinglish
    r'\bsa+ns\s+(lene\s+me|me)?\s*(dikkat|takleef|problem|fulna|foolna)\b',
    r'\bgale\s*(me)?\s*(sujan|soojan|kharash|band|dabav)\b',
    r'\b(jeebh|honth|chehre)\s*(par|pe|me)?\s*(sujan|soojan)\b',
    r'\b(behosh(i)?|chakkar|chhati\s+me\s+dard)\b',
    r'\bdam\s+ghut(na|raha|ne)?\b',
]


# ==============================================================================
# Custom Exceptions
# ==============================================================================

class AllergyCheckerError(Exception):
    """Base exception for allergy checker service."""
    pass


class AllergyCheckerValidationError(AllergyCheckerError):
    """Raised when user input fails validation (size, MIME type, empty inputs)."""
    pass


class AllergyCheckerConfigError(AllergyCheckerError):
    """Raised when Groq API key is missing or invalid."""
    pass


class AllergyCheckerRateLimitError(AllergyCheckerError):
    """Raised when public endpoint rate limit is exceeded."""
    pass


class AllergyCheckerTimeoutError(AllergyCheckerError):
    """Raised when external Groq API call times out."""
    pass


class AllergyCheckerAPIError(AllergyCheckerError):
    """Raised when external Groq API returns an unrecoverable error."""
    pass


class AllergyCheckerModelError(AllergyCheckerAPIError):
    """Raised when the requested Groq model is invalid, decommissioned, or not found."""
    pass


class AllergyCheckerAuthError(AllergyCheckerAPIError):
    """Raised when Groq API authentication fails."""
    pass


# ==============================================================================
# Helper Functions & Heuristics
# ==============================================================================

def _extract_groq_error_details(exc: Exception) -> Tuple[Optional[int], Optional[str]]:
    """
    Safely extracts HTTP status code and Groq error code without sensitive data.
    Ensures zero leakage of API keys, symptoms, or images in server logs.
    """
    status_code = getattr(exc, 'status_code', None)
    err_code = None
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        err_dict = body.get('error')
        if isinstance(err_dict, dict):
            err_code = err_dict.get('code')
    if not err_code:
        err_code = getattr(exc, 'code', None)
    return status_code, err_code

def check_rate_limit(client_ip: str, limit: int = 15, window_seconds: int = 60) -> bool:
    """
    Enforces basic public IP rate limiting using Django cache.
    Allows up to `limit` requests per `window_seconds`.
    """
    if not client_ip:
        client_ip = '127.0.0.1'
    cache_key = f"allergy_checker_rate_limit:{client_ip}"
    try:
        current_count = cache.get(cache_key, 0)
        if current_count >= limit:
            return False
        cache.set(cache_key, current_count + 1, window_seconds)
        return True
    except Exception as exc:
        logger.warning("Cache rate limit lookup failed: %s. Permitting request.", exc)
        return True


def detect_emergency_symptoms(symptoms_text: str) -> bool:
    """
    Deterministic server-side regex check for critical anaphylactic / airway symptoms
    in English, Hindi, and Hinglish. Guarantees prompt emergency alerting.
    """
    if not symptoms_text:
        return False
    lower_text = symptoms_text.lower()
    for pattern in EMERGENCY_KEYWORDS:
        if re.search(pattern, lower_text):
            return True
    return False


def validate_image_upload(image_file: Any) -> Tuple[bytes, str]:
    """
    Validates uploaded file size and content type.
    Returns (raw_bytes, normalized_mime_type).
    """
    if not image_file:
        raise AllergyCheckerValidationError("No image file provided.")

    size = getattr(image_file, 'size', None)
    if size is not None and size > MAX_IMAGE_SIZE_BYTES:
        raise AllergyCheckerValidationError(
            f"Image size exceeds the 5 MB limit (file size: {round(size / (1024 * 1024), 2)} MB). "
            "Please upload a smaller image."
        )

    content_type = getattr(image_file, 'content_type', '') or ''
    normalized_mime = ALLOWED_IMAGE_MIME_TYPES.get(content_type.lower())

    # Fallback to file extension if content_type is generic
    if not normalized_mime:
        name = getattr(image_file, 'name', '').lower()
        if name.endswith(('.jpg', '.jpeg')):
            normalized_mime = 'image/jpeg'
        elif name.endswith('.png'):
            normalized_mime = 'image/png'
        elif name.endswith('.webp'):
            normalized_mime = 'image/webp'

    if not normalized_mime:
        raise AllergyCheckerValidationError(
            "Unsupported file format. Please upload a medicine photo in JPG, JPEG, PNG, or WEBP format."
        )

    try:
        if hasattr(image_file, 'read'):
            raw_bytes = image_file.read()
            if hasattr(image_file, 'seek'):
                image_file.seek(0)
        elif isinstance(image_file, bytes):
            raw_bytes = image_file
        else:
            raise AllergyCheckerValidationError("Invalid image data received.")

        if len(raw_bytes) > MAX_IMAGE_SIZE_BYTES:
            raise AllergyCheckerValidationError(
                "Image size exceeds the 5 MB limit. Please upload a smaller image."
            )
        if len(raw_bytes) == 0:
            raise AllergyCheckerValidationError("Uploaded image file is empty.")

        return raw_bytes, normalized_mime
    except AllergyCheckerValidationError:
        raise
    except Exception as exc:
        logger.error("Failed to read uploaded image bytes: %s", exc)
        raise AllergyCheckerValidationError("Failed to read the uploaded image. Please try again.")


# ==============================================================================
# Groq Analysis Core Service
# ==============================================================================

def get_system_prompt() -> str:
    """
    Defines strict clinical instructions and JSON schema for Groq inference.
    """
    return (
        "You are an expert Clinical Pharmacologist and AI Allergy Screening Assistant for MedAccess. "
        "Your duty is to evaluate whether a user's reported adverse symptoms following medication use "
        "could be consistent with a known drug hypersensitivity or allergic reaction.\n\n"
        "STRICT CLINICAL SAFETY RULES (NEVER VIOLATE):\n"
        "1. NEVER claim to diagnose an allergy or provide medical certainty.\n"
        "2. NEVER guarantee that a medicine is safe or that no allergic reaction will occur.\n"
        "3. NEVER recommend taking another dose to 'test' for an allergy.\n"
        "4. NEVER recommend self-medication or altering doctor-prescribed treatment without medical guidance.\n"
        "5. Distinguish true drug hypersensitivity (IgE/non-IgE: urticaria/hives, angioedema, bronchospasm, "
        "anaphylaxis, blistering) from common pharmacological side effects (e.g. mild nausea, headache, drowsiness, gastric upset).\n"
        "6. If the medicine packaging or name is blurry, ambiguous, or cannot be identified with confidence, "
        "explicitly state that the medicine is UNIDENTIFIED and analysis is limited.\n"
        "7. For severe warning signs (breathing difficulty, throat/lip/tongue swelling, severe dizziness, syncope, "
        "rapidly spreading rash, blistering), set is_emergency to true, risk_level to 'EMERGENCY', and prominently advise "
        "immediate emergency hospital evaluation.\n"
        "8. Respond in clear, simple language matching the user's input language (English, Hindi, or Hinglish as appropriate).\n\n"
        "OUTPUT FORMAT: You MUST return a single valid JSON object with EXACTLY this structure:\n"
        "{\n"
        '  "identified_medicine": "string or null",\n'
        '  "active_ingredient": "string or null (e.g. Amoxicillin, Paracetamol, Ibuprofen)",\n'
        '  "identification_confidence": "HIGH" | "MEDIUM" | "LOW" | "UNIDENTIFIED",\n'
        '  "user_symptoms": ["string", "string"],\n'
        '  "is_emergency": true | false,\n'
        '  "risk_level": "EMERGENCY" | "POTENTIAL_ALLERGY" | "COMMON_SIDE_EFFECT" | "UNLIKELY_ALLERGY" | "INSUFFICIENT_DATA",\n'
        '  "analysis_summary": "Concise 2-3 sentence overview of findings in the user\'s language",\n'
        '  "allergy_assessment": "Detailed assessment of whether symptoms align with drug allergy vs ordinary side effect",\n'
        '  "side_effect_differentiation": "Clear comparison between an allergic response and standard non-allergic side effects for this drug",\n'
        '  "known_reaction_info": "Established clinical allergy profile or cross-reactivity for this medicine class",\n'
        '  "warning_signs": ["Red flag symptom 1", "Red flag symptom 2"],\n'
        '  "recommended_actions": ["Action 1 (e.g. Stop taking medication and contact doctor)", "Action 2"],\n'
        '  "emergency_guidance": "Clear emergency instructions if severe symptoms are suspected, else null"\n'
        "}"
    )


def analyze_medicine_allergy(
    medicine_name: Optional[str] = None,
    image_file: Optional[Any] = None,
    symptoms: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for screening medicine allergy via Groq API.
    Accepts medicine_name (text), image_file (upload), or both, alongside symptoms.
    Returns a structured dictionary with clinical assessment, risk level, and next steps.
    """
    # 1. Rate Limiting Check
    if client_ip and not check_rate_limit(client_ip, limit=20, window_seconds=60):
        raise AllergyCheckerRateLimitError(
            "You have made too many screening requests in a short time. "
            "Please wait a moment before trying again."
        )

    # 2. Input Validation
    clean_med_name = (medicine_name or '').strip()
    clean_symptoms = (symptoms or '').strip()

    if not clean_med_name and not image_file:
        raise AllergyCheckerValidationError(
            "Please provide at least one medicine detail: enter the medicine name or upload a packaging/prescription image."
        )

    if not clean_symptoms:
        raise AllergyCheckerValidationError(
            "Please describe the symptoms you or the patient experienced after taking the medicine."
        )

    if len(clean_symptoms) < 3:
        raise AllergyCheckerValidationError(
            "Please provide a more descriptive symptom detail (e.g., itching, rashes, swelling, vomiting)."
        )

    # 3. Emergency heuristic detection (immediate safety check)
    immediate_emergency = detect_emergency_symptoms(clean_symptoms)

    # 4. Process Image (if provided)
    base64_image: Optional[str] = None
    image_mime: Optional[str] = None
    if image_file:
        raw_bytes, image_mime = validate_image_upload(image_file)
        base64_image = base64.b64encode(raw_bytes).decode('utf-8')

    # 5. Groq API Configuration
    groq_api_key = getattr(settings, 'GROQ_API_KEY', '') or ''
    if not groq_api_key:
        logger.error("GROQ_API_KEY is not configured in settings/environment.")
        # If API key is missing in development, deliver safe heuristic response
        return _build_fallback_response(
            medicine_name=clean_med_name,
            symptoms=clean_symptoms,
            is_emergency=immediate_emergency,
            reason="AI screening service key is not configured. Displaying general medical guidance."
        )

    # Determine Model: default to qwen/qwen3.8-27b for BOTH text and multimodal vision
    default_model = getattr(settings, 'GROQ_MODEL', 'qwen/qwen3.8-27b') or 'qwen/qwen3.8-27b'
    vision_model = getattr(settings, 'GROQ_VISION_MODEL', default_model) or default_model
    text_model = getattr(settings, 'GROQ_TEXT_MODEL', default_model) or default_model

    # Prevent legacy decommissioned IDs from causing failures if still present in environment
    if vision_model in ('llama-3.2-11b-vision-preview', 'llama-3.3-70b-versatile'):
        vision_model = 'qwen/qwen3.8-27b'
    if text_model in ('llama-3.2-11b-vision-preview', 'llama-3.3-70b-versatile'):
        text_model = 'qwen/qwen3.8-27b'

    selected_model = vision_model if base64_image else text_model

    # 6. Build Prompts
    system_prompt = get_system_prompt()

    user_text_content = (
        f"PATIENT REPORTED DETAILS:\n"
        f"- Medicine Name Provided: {clean_med_name if clean_med_name else 'Not specified as text (examine attached image)'}\n"
        f"- Symptoms Experienced After Taking Medicine: {clean_symptoms}\n"
        f"- Immediate Emergency Flag (Server-Side Heuristic): {'YES' if immediate_emergency else 'NO'}\n\n"
        "Please inspect the medicine details (and attached image if present), verify active ingredients, "
        "and analyze if the reported symptoms suggest a true drug hypersensitivity or ordinary side effects."
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]

    if base64_image and image_mime:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_text_content},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_mime};base64,{base64_image}"
                    }
                }
            ]
        })
    else:
        messages.append({
            "role": "user",
            "content": user_text_content
        })

    # 7. Execute Groq API Call
    try:
        from groq import (
            Groq,
            APITimeoutError,
            RateLimitError,
            AuthenticationError,
            APIConnectionError,
            BadRequestError,
            NotFoundError,
            APIStatusError,
        )
    except ImportError:
        logger.error("groq package is not installed.")
        return _build_fallback_response(
            medicine_name=clean_med_name,
            symptoms=clean_symptoms,
            is_emergency=immediate_emergency,
            reason="Groq library is not installed in the active environment."
        )

    try:
        client = Groq(api_key=groq_api_key, timeout=30.0)
        completion = client.chat.completions.create(
            model=selected_model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1500,
        )

        response_text = completion.choices[0].message.content or "{}"
        parsed_data = json.loads(response_text)

        # Merge with server-side emergency heuristic for patient safety
        if immediate_emergency:
            parsed_data['is_emergency'] = True
            if parsed_data.get('risk_level') != 'EMERGENCY':
                parsed_data['risk_level'] = 'EMERGENCY'
            if not parsed_data.get('emergency_guidance'):
                parsed_data['emergency_guidance'] = (
                    "Potentially serious symptoms (such as breathing difficulty or severe swelling) detected. "
                    "Seek immediate emergency medical evaluation or visit the nearest emergency department right away."
                )

        # Attach standard fields
        parsed_data['disclaimer'] = MANDATORY_MEDICAL_DISCLAIMER
        parsed_data['submitted_medicine_name'] = clean_med_name
        parsed_data['submitted_symptoms'] = clean_symptoms
        parsed_data['has_image'] = bool(base64_image)

        return parsed_data

    except AuthenticationError as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.error("Groq API authentication failure [%s]: status=%s, code=%s", type(exc).__name__, status_code, err_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI screening authentication unavailable. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerAuthError("AI screening authentication failed. Please verify API configuration with MedAccess administration.")

    except RateLimitError as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.warning("Groq API quota/rate limit reached [%s]: status=%s, code=%s", type(exc).__name__, status_code, err_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI screening service busy. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerRateLimitError("The AI allergy screening service is currently experiencing high demand or quota limits. Please try again in a few moments.")

    except APITimeoutError as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.warning("Groq API request timed out [%s]: status=%s", type(exc).__name__, status_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI screening service timed out. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerTimeoutError("The screening request timed out while contacting the AI service. Please check your internet connection and try again.")

    except APIConnectionError as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.error("Groq API connection error [%s]: status=%s", type(exc).__name__, status_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI screening connection unavailable. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerAPIError("Unable to establish a connection to the AI analysis provider. Please check your network and try again.")

    except (NotFoundError, BadRequestError) as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.error("Groq API model/request error [%s]: status=%s, code=%s", type(exc).__name__, status_code, err_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI screening model unavailable. Immediate emergency guidance displayed."
            )
        if err_code in ('model_decommissioned', 'model_not_found') or 'model' in str(err_code or '').lower():
            raise AllergyCheckerModelError("The configured AI model is decommissioned or not available. Please verify model configuration.")
        raise AllergyCheckerAPIError("An invalid request was sent to the AI screening service. Please verify your inputs and try again.")

    except APIStatusError as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.error("Groq API status error [%s]: status=%s, code=%s", type(exc).__name__, status_code, err_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="AI service status error. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerAPIError("The AI screening service returned an error status. Please try again shortly.")

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON response from Groq: %s", type(exc).__name__)
        return _build_fallback_response(
            medicine_name=clean_med_name,
            symptoms=clean_symptoms,
            is_emergency=immediate_emergency,
            reason="The AI response could not be formatted properly. Please consult a doctor."
        )

    except Exception as exc:
        status_code, err_code = _extract_groq_error_details(exc)
        logger.error("Unexpected error during Groq allergy analysis [%s]: status=%s, code=%s", type(exc).__name__, status_code, err_code)
        if immediate_emergency:
            return _build_fallback_response(
                medicine_name=clean_med_name,
                symptoms=clean_symptoms,
                is_emergency=True,
                reason="Unexpected AI screening error. Immediate emergency guidance displayed."
            )
        raise AllergyCheckerAPIError("An unexpected error occurred while analyzing your medication details. Please try again or consult a doctor.")


def _build_fallback_response(
    medicine_name: str,
    symptoms: str,
    is_emergency: bool,
    reason: str = ""
) -> Dict[str, Any]:
    """
    Constructs a safe, clinical fallback response when external AI is unavailable or unconfigured.
    Guarantees that emergency alerts and basic precautions are always communicated.
    """
    risk_level = "EMERGENCY" if is_emergency else "INSUFFICIENT_DATA"

    warning_signs = [
        "Difficulty breathing, shortness of breath, or wheezing",
        "Swelling of the lips, tongue, face, or throat",
        "Severe dizziness, lightheadedness, or loss of consciousness",
        "Rapidly spreading hives, intense itching, or skin peeling",
    ]

    recommended_actions = [
        "Stop taking the medication immediately if an adverse reaction is suspected.",
        "Contact the prescribing doctor or a licensed pharmacist right away.",
        "Never take a second dose to test whether you are allergic.",
        "Keep the medicine packaging ready to show medical personnel.",
    ]

    emergency_guidance = (
        "IMMEDIATE EMERGENCY ACTION REQUIRED: Your reported symptoms may indicate a severe allergic "
        "or anaphylactic reaction. Call your local emergency helpline (112 or local ambulance) or proceed "
        "to the nearest hospital emergency room immediately."
    ) if is_emergency else None

    return {
        "identified_medicine": medicine_name or "Unidentified Medication",
        "active_ingredient": None,
        "identification_confidence": "UNIDENTIFIED" if not medicine_name else "LOW",
        "user_symptoms": [symptoms] if symptoms else [],
        "is_emergency": is_emergency,
        "risk_level": risk_level,
        "analysis_summary": (
            "Screening service operates in clinical safety fallback mode. "
            "Your symptoms require personal evaluation by a qualified doctor or pharmacist."
        ),
        "allergy_assessment": (
            "Because automated AI screening could not be completed, this tool cannot evaluate whether "
            "your symptoms stem from a drug allergy or a common side effect. Please seek direct medical advice."
        ),
        "side_effect_differentiation": (
            "True drug allergies involve the immune system and can cause rashes, hives, swelling, or breathing distress. "
            "Common non-allergic side effects often include mild stomach upset, nausea, or drowsiness."
        ),
        "known_reaction_info": (
            "Medications across various drug classes can trigger hypersensitivity reactions in susceptible individuals."
        ),
        "warning_signs": warning_signs,
        "recommended_actions": recommended_actions,
        "emergency_guidance": emergency_guidance,
        "disclaimer": MANDATORY_MEDICAL_DISCLAIMER,
        "submitted_medicine_name": medicine_name,
        "submitted_symptoms": symptoms,
        "fallback_notice": reason or "Clinical safety fallback activated.",
    }
