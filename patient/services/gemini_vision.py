"""
MedAccess Gemini Vision Prescription Extraction Service (Phase 3, Task 3B).

Provides a secure, robust service to extract structured prescription details
(medications, dosages, frequencies, verification flags, and unclear details)
from preprocessed prescription images and multi-page PDFs using Google Gemini Vision.

Key Capabilities:
- Secure API key & model retrieval from Django settings (loaded via .env).
- Strict adherence to the configured model (gemini-3.5-flash-lite) with zero silent fallbacks.
- Multi-page sequential processing using the official Google GenAI SDK (google-genai).
- Pydantic structured output schema enforcement (response_schema).
- Deterministic extraction with temperature=0.0.
- Rigorous clinical safety system instructions:
    * Zero hallucination
    * No guessing or extrapolating
    * No automatic drug name corrections
    * Explicit 'needs_verification' flagging with clear reasons
    * Segregated 'unclear_details' for unreadable or ambiguous writing
- Independent Python runtime validation for structure and field types.
- Strict credential redaction: API keys are never exposed in logs or exceptions.
- Custom exception hierarchy mapping API, network, auth, quota, and schema failures.
"""

from dataclasses import dataclass, field
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

from django.conf import settings
from google import genai
from google.genai import errors, types
import httpx
from pydantic import BaseModel, Field

from patient.services.prescription_processor import ProcessedPage, ProcessedPrescription

logger = logging.getLogger(__name__)

# --- Default Model Configuration ---
DEFAULT_GEMINI_MODEL: str = "gemini-3.5-flash-lite"


# --- Custom Exception Hierarchy ---
class GeminiVisionError(Exception):
    """Base exception for all Gemini Vision prescription extraction errors."""
    pass


class GeminiConfigurationError(GeminiVisionError):
    """Raised when Gemini API key or model configuration is missing or invalid."""
    pass


class GeminiAuthenticationError(GeminiVisionError):
    """Raised when Gemini API authentication fails (invalid or unauthorized API key)."""
    pass


class GeminiQuotaError(GeminiVisionError):
    """Raised when Gemini API quota or rate limits are exceeded."""
    pass


class GeminiNetworkError(GeminiVisionError):
    """Raised when network connectivity, connection timeout, or DNS resolution fails."""
    pass


class GeminiModelError(GeminiVisionError):
    """Raised when the specified Gemini model is not found, unsupported, or service is unavailable."""
    pass


class GeminiExtractionError(GeminiVisionError):
    """Raised when Gemini returns an empty response, unparseable output, or general extraction failure."""
    pass


class GeminiValidationError(GeminiVisionError):
    """Raised when Gemini output violates the expected schema or field types."""
    pass


# --- Structured Output Schema (Pydantic) ---
class MedicineExtraction(BaseModel):
    """
    Extracted medication details with medical safety verification flags.
    Every field is mandatory in the structured JSON output.
    """
    medicine_name: str = Field(
        default="",
        description="Exact brand or generic name written on prescription without dosage/frequency. Do not correct or guess."
    )
    strength: str = Field(
        default="",
        description="Potency or strength with unit if written (e.g., '500 mg', '50 mcg', '10 ml', '0.5%'). Empty string if not stated."
    )
    dosage: str = Field(
        default="",
        description="Unit dose per intake (e.g., '1 tablet', '1 capsule', '2 puffs', '5 ml'). Extract from regimen if written; empty string if not stated."
    )
    frequency: str = Field(
        default="",
        description="Intake schedule or pattern (e.g., '1-0-1', '0-0-1', '1-1-1', 'Once daily', 'Twice daily', 'OD', 'BD', 'TDS', 'HS', 'SOS'). Empty string if not stated."
    )
    duration: str = Field(
        default="",
        description="Course length or duration (e.g., '5 days', '7 days', '2 weeks', '1 month', 'x 5d'). Empty string if not stated."
    )
    quantity: str = Field(
        default="",
        description="Total dispensed/prescribed pack or unit count if explicitly stated (e.g., '10 tablets', '#30', '1 bottle'). Empty string if not explicitly stated."
    )
    instructions: str = Field(
        default="",
        description="Timing, route, or food instructions (e.g., 'After food', 'Before breakfast', 'PO'). Empty string if not stated."
    )
    needs_verification: bool = Field(
        default=False,
        description="True if handwritten text is faint, ambiguous, partly illegible, or doubtful."
    )
    verification_reason: str = Field(
        default="",
        description="Explanation why verification is required. Empty string if needs_verification is false."
    )


class PrescriptionExtraction(BaseModel):
    """
    Complete structured prescription extraction conforming to MedAccess clinical schema.
    """
    medicines: List[MedicineExtraction] = Field(
        default_factory=list,
        description="List of all extracted medicines found in the prescription pages."
    )
    unclear_details: List[str] = Field(
        default_factory=list,
        description="List of illegible, ambiguous, cut-off, or unidentifiable handwritten notes, lines, or stamps."
    )


# --- Extraction Result Container ---
@dataclass
class GeminiExtractionResult:
    """
    Structured container for validated Gemini Vision extraction results.
    """
    medicines: List[Dict[str, Any]]
    unclear_details: List[str]
    raw_json: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# --- Clinical Medical Safety System Instruction ---
CLINICAL_PRESCRIPTION_SYSTEM_INSTRUCTION: str = (
    "You are an expert clinical medical prescription transcription and verification assistant.\n"
    "Your sole purpose is to analyze the provided doctor's handwritten or printed prescription "
    "document image(s) and extract structured medication information with the highest degree of "
    "accuracy and patient safety.\n\n"
    "CRITICAL MEDICAL SAFETY RULES:\n"
    "1. ZERO HALLUCINATION: Never invent, extrapolate, guess, or assume medications, dosages, "
    "frequencies, or durations that are not visibly written in the document.\n"
    "2. NO AUTO-CORRECTION: Never 'correct' a misspelled or ambiguous medication name to a known "
    "drug name. Transcribe only what is legible. If a drug name is unclear, transcribe the visible "
    "characters, mark 'needs_verification': true, and explain the ambiguity in 'verification_reason'.\n"
    "3. VERIFICATION FLAGGING: If any detail of a medicine (name, strength, dosage, frequency, duration, "
    "instructions) is smudged, faint, partially cut off, written in non-standard shorthand, or difficult "
    "to decipher, set 'needs_verification': true and provide a specific, concise 'verification_reason'. "
    "If all details for that medicine are clearly legible and certain, set 'needs_verification': false "
    "and 'verification_reason': ''.\n"
    "4. UNCLEAR DETAILS: If there are handwritten notes, stamps, unidentifiable doctor instructions, "
    "illegible signatures, or lines that cannot be attributed with certainty to a specific medication, "
    "record each item in 'unclear_details'.\n"
    "5. MULTI-PAGE CONTINUATIONS: Analyze all pages in their sequential order. If medication lists span "
    "multiple pages, produce a single consolidated list without duplicate entries.\n"
    "6. COMPLETE REGIMEN EXTRACTION: Carefully inspect all columns, right-hand margins, and shorthand "
    "grids on the prescription so visible strength ('50 mcg', '10 mg'), per-intake dosage ('1 tab'), "
    "frequency ('1-0-1', '0-0-1', 'OD', 'BD', 'TDS', 'HS'), duration ('5 days', 'x 10d', '1 month'), "
    "explicit quantity ('#15', '10 tabs'), and food/timing instructions are captured in their respective "
    "fields rather than omitted or collapsed together.\n"
    "7. FORMAT: Output strictly valid JSON conforming to the requested schema. Return empty strings "
    "for fields not explicitly stated in the prescription. Return an empty 'medicines' array if no "
    "medications are present in the document."
)

PRESCRIPTION_EXTRACTION_USER_PROMPT: str = (
    "Please read the attached prescription page(s) in sequential order and extract all prescribed medicines "
    "and any unclear or illegible text according to the required clinical JSON schema."
)


# --- Security & Credential Redaction ---
def redact_sensitive_text(text: str) -> str:
    """
    Sanitizes log messages and exception strings to prevent leaking the Gemini API key.
    """
    if not text:
        return ""

    sanitized = str(text)

    # Redact configured API key if present in settings
    configured_key = getattr(settings, "GEMINI_API_KEY", None)
    if configured_key and isinstance(configured_key, str) and len(configured_key) > 4:
        sanitized = sanitized.replace(configured_key, "[REDACTED_API_KEY]")

    # Redact Google API key pattern (AIza...)
    sanitized = re.sub(r"AIza[0-9A-Za-z\-_]{35}", "[REDACTED_API_KEY]", sanitized)

    # Redact query params containing key=...
    sanitized = re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED_API_KEY]", sanitized)

    return sanitized


# --- Independent Output Validation ---
REQUIRED_MEDICINE_FIELDS = {
    "medicine_name",
    "strength",
    "dosage",
    "frequency",
    "duration",
    "instructions",
    "needs_verification",
    "verification_reason",
}

STRING_MEDICINE_FIELDS = {
    "medicine_name",
    "strength",
    "dosage",
    "frequency",
    "duration",
    "instructions",
    "verification_reason",
}


def validate_extracted_prescription_data(data: Any) -> Dict[str, Any]:
    """
    Independently validates the extracted JSON structure against the required schema.
    Ensures all 8 medicine fields are present, types are valid, and unclear_details is a list of strings.

    Raises:
        GeminiValidationError: If the structure or data types violate the schema.

    Returns:
        Dict[str, Any]: Validated, sanitized data dictionary.
    """
    if not isinstance(data, dict):
        raise GeminiValidationError(
            f"Extracted prescription data must be a JSON object (dict), got {type(data).__name__}."
        )

    # Check root keys
    if "medicines" not in data:
        raise GeminiValidationError("Extracted data is missing the required 'medicines' list.")
    if "unclear_details" not in data:
        raise GeminiValidationError("Extracted data is missing the required 'unclear_details' list.")

    medicines = data["medicines"]
    if not isinstance(medicines, list):
        raise GeminiValidationError(
            f"'medicines' must be a list, got {type(medicines).__name__}."
        )

    unclear_details = data["unclear_details"]
    if not isinstance(unclear_details, list):
        raise GeminiValidationError(
            f"'unclear_details' must be a list, got {type(unclear_details).__name__}."
        )

    # Validate unclear_details items
    validated_unclear: List[str] = []
    for idx, item in enumerate(unclear_details):
        if not isinstance(item, str):
            raise GeminiValidationError(
                f"unclear_details[{idx}] must be a string, got {type(item).__name__}."
            )
        validated_unclear.append(item.strip())

    # Validate medicines items
    validated_medicines: List[Dict[str, Any]] = []
    for idx, med in enumerate(medicines):
        if not isinstance(med, dict):
            raise GeminiValidationError(
                f"medicines[{idx}] must be a dictionary, got {type(med).__name__}."
            )

        # Check required fields
        missing_fields = REQUIRED_MEDICINE_FIELDS - set(med.keys())
        if missing_fields:
            sorted_missing = sorted(list(missing_fields))
            raise GeminiValidationError(
                f"medicines[{idx}] is missing required fields: {', '.join(sorted_missing)}."
            )

        # Validate types
        for field_name in STRING_MEDICINE_FIELDS:
            val = med[field_name]
            if not isinstance(val, str):
                raise GeminiValidationError(
                    f"medicines[{idx}].{field_name} must be a string, got {type(val).__name__}."
                )

        needs_verification = med["needs_verification"]
        if not isinstance(needs_verification, bool):
            raise GeminiValidationError(
                f"medicines[{idx}].needs_verification must be a boolean, got {type(needs_verification).__name__}."
            )

        # Normalize clean record
        item_record = {
            "medicine_name": med["medicine_name"].strip(),
            "strength": med["strength"].strip(),
            "dosage": med["dosage"].strip(),
            "frequency": med["frequency"].strip(),
            "duration": med["duration"].strip(),
            "instructions": med["instructions"].strip(),
            "needs_verification": needs_verification,
            "verification_reason": med["verification_reason"].strip(),
        }
        if "quantity" in med:
            if not isinstance(med["quantity"], str):
                raise GeminiValidationError(
                    f"medicines[{idx}].quantity must be a string, got {type(med['quantity']).__name__}."
                )
            item_record["quantity"] = med["quantity"].strip()
        validated_medicines.append(item_record)

    return {
        "medicines": validated_medicines,
        "unclear_details": validated_unclear,
    }


# --- Page Normalization Helper ---
def _normalize_pages(
    prescription_input: Union[ProcessedPrescription, List[ProcessedPage], ProcessedPage]
) -> List[ProcessedPage]:
    """
    Normalizes ProcessedPrescription, list of ProcessedPage, or single ProcessedPage
    into a sequential list of ProcessedPage objects.
    """
    if prescription_input is None:
        raise GeminiValidationError("No prescription input provided for extraction.")

    if isinstance(prescription_input, ProcessedPrescription):
        pages = prescription_input.pages
    elif isinstance(prescription_input, list):
        pages = prescription_input
    elif isinstance(prescription_input, ProcessedPage):
        pages = [prescription_input]
    else:
        raise GeminiValidationError(
            f"Unsupported prescription input type: {type(prescription_input).__name__}. "
            "Expected ProcessedPrescription or list of ProcessedPage."
        )

    if not pages:
        raise GeminiValidationError("Prescription contains no processed pages to extract.")

    # Validate that every item is a ProcessedPage
    for idx, page in enumerate(pages):
        if not isinstance(page, ProcessedPage):
            raise GeminiValidationError(
                f"Page item at index {idx} is of type {type(page).__name__}, expected ProcessedPage."
            )
        if not page.image_bytes:
            raise GeminiValidationError(
                f"Page {getattr(page, 'page_number', idx + 1)} contains empty image bytes."
            )

    return sorted(pages, key=lambda p: p.page_number)


# --- Core Gemini Vision Extraction Service ---
def extract_prescription_details(
    prescription_input: Union[ProcessedPrescription, List[ProcessedPage], ProcessedPage],
    client: Optional[Any] = None,
) -> GeminiExtractionResult:
    """
    Extracts structured prescription data from preprocessed prescription pages using Google Gemini Vision.

    Args:
        prescription_input: ProcessedPrescription or list of ProcessedPage objects.
        client: Optional genai.Client instance for testing / dependency injection.

    Returns:
        GeminiExtractionResult containing medicines, unclear_details, raw_json, and metadata.

    Raises:
        GeminiConfigurationError: When API key is missing or invalid in settings.
        GeminiAuthenticationError: When API authentication fails (401/403).
        GeminiQuotaError: When rate limits or quotas are exceeded (429).
        GeminiNetworkError: When network connectivity or timeout occurs.
        GeminiModelError: When the model is unsupported or service is down (404/5xx).
        GeminiExtractionError: When output cannot be parsed or empty response is returned.
        GeminiValidationError: When extracted JSON violates the required medical schema.
    """
    # 1. Configuration Check
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        raise GeminiConfigurationError(
            "Gemini API key is not configured in settings. Please set GEMINI_API_KEY in your .env file."
        )

    model_name = getattr(settings, "GEMINI_MODEL", None)
    if not model_name or not isinstance(model_name, str) or not model_name.strip():
        model_name = DEFAULT_GEMINI_MODEL

    # 2. Normalize and order pages
    pages = _normalize_pages(prescription_input)

    # 3. Build Multimodal Content
    contents: List[Any] = []
    for page in pages:
        part = types.Part.from_bytes(
            data=page.image_bytes,
            mime_type=page.mime_type
        )
        contents.append(part)

    contents.append(PRESCRIPTION_EXTRACTION_USER_PROMPT)

    # 4. Configure Generation with Strict Schema
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=PrescriptionExtraction,
        system_instruction=CLINICAL_PRESCRIPTION_SYSTEM_INSTRUCTION,
        temperature=0.0,
    )

    # 5. Initialize Client if not provided
    if client is None:
        client = genai.Client(api_key=api_key.strip())

    # 6. Execute Model Call with Comprehensive Exception Handling
    start_time = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )
    except errors.ClientError as exc:
        code = getattr(exc, "code", None)
        message = redact_sensitive_text(str(exc))
        if code in (401, 403):
            raise GeminiAuthenticationError(
                f"Gemini API authentication failed (HTTP {code}). Please verify GEMINI_API_KEY."
            ) from exc
        if code == 429:
            raise GeminiQuotaError(
                "Gemini API rate limit or quota exceeded (HTTP 429). Please try again later."
            ) from exc
        if code == 404:
            raise GeminiModelError(
                f"Gemini model '{model_name}' was not found or is not supported (HTTP 404)."
            ) from exc
        raise GeminiExtractionError(f"Gemini client error (HTTP {code}): {message}") from exc

    except errors.ServerError as exc:
        code = getattr(exc, "code", None)
        message = redact_sensitive_text(str(exc))
        raise GeminiModelError(
            f"Gemini server error ({code or '5xx'}): The AI service is currently unavailable. {message}"
        ) from exc

    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        message = redact_sensitive_text(str(exc))
        if code in (401, 403):
            raise GeminiAuthenticationError(
                f"Gemini API authentication failed (HTTP {code})."
            ) from exc
        if code == 429:
            raise GeminiQuotaError(
                "Gemini API rate limit or quota exceeded (HTTP 429)."
            ) from exc
        if code == 404:
            raise GeminiModelError(
                f"Gemini model '{model_name}' was not found (HTTP 404)."
            ) from exc
        raise GeminiExtractionError(f"Gemini API error (HTTP {code}): {message}") from exc

    except (httpx.TimeoutException, TimeoutError) as exc:
        raise GeminiNetworkError(
            f"Request to Gemini API timed out while processing prescription: {redact_sensitive_text(str(exc))}"
        ) from exc

    except (httpx.NetworkError, httpx.ConnectError, ConnectionError) as exc:
        raise GeminiNetworkError(
            f"Network connectivity error while contacting Gemini API: {redact_sensitive_text(str(exc))}"
        ) from exc

    except Exception as exc:
        # Re-raise if it's already one of our custom exceptions
        if isinstance(exc, GeminiVisionError):
            raise
        raise GeminiExtractionError(
            f"Unexpected error during Gemini Vision extraction: {redact_sensitive_text(str(exc))}"
        ) from exc

    duration = time.perf_counter() - start_time

    # 7. Extract Text Content & Sanitize
    raw_text = getattr(response, "text", None)
    if not raw_text or not raw_text.strip():
        raise GeminiExtractionError(
            "Gemini returned an empty or blank response for the prescription."
        )

    clean_text = raw_text.strip()
    # Remove markdown formatting fences if present defensively
    if clean_text.startswith("```"):
        clean_text = re.sub(r"^```[a-zA-Z]*\n?", "", clean_text)
        clean_text = re.sub(r"\n?```$", "", clean_text).strip()

    # 8. Parse JSON
    try:
        parsed_json = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        raise GeminiExtractionError(
            f"Failed to parse Gemini response as valid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}."
        ) from exc

    # 9. Perform Independent Schema & Type Validation
    validated_data = validate_extracted_prescription_data(parsed_json)

    metadata = {
        "model": model_name,
        "page_count": len(pages),
        "duration_seconds": round(duration, 3),
    }

    return GeminiExtractionResult(
        medicines=validated_data["medicines"],
        unclear_details=validated_data["unclear_details"],
        raw_json=clean_text,
        metadata=metadata,
    )

