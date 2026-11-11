"""
MedAccess Inventory Availability Engine (Phase 1).

Compares a patient's confirmed prescription medicines against the active,
non-expired inventory of nearby APPROVED MedAccess pharmacies.

Key Capabilities:
1. parse_quantity_or_calculate_course: Safely parses explicit quantities or
   computes course totals from dosage, frequency, and duration without guessing.
2. match_prescription_medicine_to_catalogue: Strict multi-tier matching against
   the master Medicine catalogue with unit-aware strength, dosage-form, spelling,
   and single-ingredient salt gates. Never silently substitutes different brands
   or different strengths.
3. get_pharmacy_medicine_stock: Aggregates active, non-expired inventory batches
   (expiry_date >= today, quantity > 0) for a given pharmacy and medicine.
4. check_prescription_pharmacy_availability: Evaluates all confirmed prescription
   medicines against all approved pharmacies within the specified search radius.
"""

from dataclasses import dataclass, field, asdict
from decimal import Decimal
import logging
import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from django.db.models import Sum
from django.utils import timezone

from patient.models import Prescription, PrescriptionMedicine
from pharmacy.models import Medicine, Inventory, PharmacyProfile
from pharmacy.services.catalogue_matcher import (
    match_medicine_catalogue,
    normalize_token,
    parse_strength_components,
    strength_components_compatible,
    split_medicine_name_and_strength,
    extract_single_ingredient_salt,
    is_safe_spelling_match,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Data Containers
# ============================================================================

@dataclass
class MedicineMatchResult:
    """Result of matching a single prescribed medicine to the master catalogue."""
    medicine: Optional[Medicine] = None
    match_status: str = "UNMAPPED"  # MATCHED | AMBIGUOUS | UNMAPPED
    explanation: str = ""
    matched_medicines: List[Medicine] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'matched_catalogue_id': self.medicine.id if self.medicine else None,
            'matched_catalogue_name': self.medicine.medicine_name if self.medicine else None,
            'match_status': self.match_status,
            'explanation': self.explanation,
        }


@dataclass
class MedicineAvailabilityItem:
    """Availability evaluation of a single prescribed medicine at a specific pharmacy."""
    prescription_medicine_id: Optional[int]
    prescribed_name: str
    prescribed_strength: Optional[str]
    matched_catalogue_id: Optional[int]
    matched_catalogue_name: Optional[str]
    match_status: str              # MATCHED | AMBIGUOUS | UNMAPPED
    required_quantity: Optional[int]
    required_quantity_note: str
    available_quantity: int
    availability_status: str        # FULLY_AVAILABLE | PARTIALLY_AVAILABLE | IN_STOCK | OUT_OF_STOCK | NOT_STOCKED | AMBIGUOUS
    explanation: str
    prescribed_dosage: Optional[str] = None
    prescribed_frequency: Optional[str] = None
    prescribed_duration: Optional[str] = None
    prescribed_quantity: Optional[str] = None
    prescribed_instructions: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PharmacyAvailabilityReport:
    """Availability summary of an entire prescription at a specific nearby pharmacy."""
    pharmacy_id: int
    pharmacy_name: str
    phone_number: str
    address: str
    distance_km: float
    latitude: float
    longitude: float
    total_prescribed_count: int
    available_items_count: int
    overall_status: str            # FULLY_AVAILABLE | PARTIALLY_AVAILABLE | OUT_OF_STOCK
    items: List[MedicineAvailabilityItem] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['items'] = [item.as_dict() if hasattr(item, 'as_dict') else item for item in self.items]
        return data


# ============================================================================
# Helpers: Geography & Token Extraction
# ============================================================================

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two geographic coordinates
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


def extract_numeric_tokens(text: Optional[str]) -> List[str]:
    """Extracts numeric tokens (including decimals) from a string, e.g. '500mg' -> ['500'], '1.2gm' -> ['1.2']."""
    if not text:
        return []
    return re.findall(r'(\d+(?:\.\d+)?)', str(text))


# Known dosage form keyword sets for classification
DOSAGE_FORM_FAMILIES = {
    'tablet': {'tablet', 'tablets', 'tab', 'tabs', 'dt', 'chewable'},
    'capsule': {'capsule', 'capsules', 'cap', 'caps'},
    'syrup': {'syrup', 'suspension', 'liquid', 'solution', 'elixir'},
    'drops': {'drops', 'drop'},
    'injection': {'injection', 'inj', 'vial', 'ampoule'},
    'inhaler': {'inhaler', 'respules', 'rotacaps', 'puff', 'puffs'},
    'topical': {'cream', 'ointment', 'gel', 'lotion'},
}


def detect_dosage_form_family(text: Optional[str]) -> Optional[str]:
    """Detects the dosage form family (e.g. tablet, syrup) from text."""
    if not text:
        return None
    tokens = set(re.findall(r'[a-zA-Z]+', str(text).lower()))
    for family, keywords in DOSAGE_FORM_FAMILIES.items():
        if tokens & keywords:
            return family
    return None


# ============================================================================
# 1. Quantity Parser & Course Calculator
# ============================================================================

def _extract_frequency_doses(text: Optional[str]) -> Optional[int]:
    """Extract deterministic doses/day from clinical schedules and shorthands."""
    if not text:
        return None
    s = str(text).strip().lower()
    if not s:
        return None

    # Pattern A: 1-0-1, 1 - 0 - 1, 1+0+1, 1-1-1-1
    pattern_num = re.search(r'(?:^|\b)(\d+)\s*[-–+]\s*(\d+)(?:\s*[-–+]\s*(\d+))?(?:\s*[-–+]\s*(\d+))?(?:\b|$)', s)
    if pattern_num:
        parts = [int(p) for p in pattern_num.groups() if p is not None]
        total = sum(parts)
        return total if total > 0 else None

    if re.search(r'\b(?:once\s+(?:daily|a\s+day)|od|qd|every\s+24\s+hours?|q24h|hs|qhs|at\s+night|bedtime)\b', s):
        return 1
    if re.search(r'\b(?:twice\s+(?:daily|a\s+day)|bd|bid|every\s+12\s+hours?|q12h)\b', s):
        return 2
    if re.search(r'\b(?:thrice\s+(?:daily|a\s+day)|three\s+times\s+(?:daily|a\s+day)|tds|tid|every\s+8\s+hours?|q8h)\b', s):
        return 3
    if re.search(r'\b(?:four\s+times\s+(?:daily|a\s+day)|qid|every\s+6\s+hours?|q6h)\b', s):
        return 4

    times_match = re.search(r'\b(\d+)\s*(?:x|times?)\s*(?:a|per|\/)?\s*day\b', s)
    if times_match:
        val = int(times_match.group(1))
        return val if val > 0 else None

    return None


def _extract_duration_days(text: Optional[str]) -> Optional[int]:
    """Extract deterministic treatment duration in days from standard clinical shorthands."""
    if not text:
        return None
    s = str(text).strip().lower()
    if not s:
        return None

    day_match = re.search(r'\b(\d+)\s*(?:days?|d)\b', s)
    if day_match:
        days = int(day_match.group(1))
        return days if days > 0 else None

    week_match = re.search(r'\b(\d+)\s*(?:weeks?|wks?|w)\b', s)
    if week_match:
        weeks = int(week_match.group(1))
        return weeks * 7 if weeks > 0 else None

    month_match = re.search(r'\b(\d+)\s*(?:months?|mos?|mths?)\b', s)
    if month_match:
        months = int(month_match.group(1))
        return months * 30 if months > 0 else None

    # Indian clinical slash duration notation: 5/7 (5 days), 2/52 (2 weeks), 1/12 (1 month)
    slash_match = re.search(r'\b(\d+)\s*/\s*(7|52|12)\b', s)
    if slash_match:
        val = int(slash_match.group(1))
        denom = int(slash_match.group(2))
        if val <= 0:
            return None
        if denom == 7:
            return val
        if denom == 52:
            return val * 7
        return val * 30

    return None


def _parse_dose_per_intake(text: Optional[str]) -> Optional[Decimal]:
    """Parse intake dose count (supports whole numbers, 0.5, 1/2, 'half tablet')."""
    if not text:
        return None
    s = str(text).strip().lower()
    if not s:
        return None

    if re.search(r'\b(?:1\s*/\s*2|half)\b', s):
        return Decimal('0.5')

    # Strip embedded schedules like '1-0-1' so only the leading unit dose is matched
    cleaned = re.sub(r'\d+\s*[-–+]\s*\d+(?:\s*[-–+]\s*\d+){0,2}', ' ', s)
    cleaned = re.sub(r'\b\d+\s*(?:days?|d|weeks?|wks?|w|months?|mos?|mths?|mg|mcg|µg|ug|gm|g)\b', ' ', cleaned)

    dose_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:tablet|tab|capsule|cap|pill|puff|drop|unit)?s?\b', cleaned)
    if not dose_match:
        return None
    try:
        val = Decimal(dose_match.group(1))
    except Exception:
        return None
    return val if val > 0 else None


def parse_quantity_or_calculate_course(med: Any) -> Tuple[Optional[int], str]:
    """
    Determines required medicine quantity from a prescription medicine record.

    Priority:
    1. Explicit prescribed quantity when present (e.g. '10 tablets', '1 bottle', '30').
    2. Computed course: dosage x frequency x duration when all three are unambiguous.
       Supports compound regimen notes across dosage/frequency/duration/instructions
       without ever inventing missing details.
    3. Returns (None, explanation) for SOS/PRN, liquid volumes, or missing terms.
    """
    # 1. Check explicit quantity
    raw_qty = getattr(med, 'quantity', None)
    if raw_qty is None and isinstance(med, dict):
        raw_qty = med.get('quantity')

    if raw_qty:
        clean_qty = str(raw_qty).strip()
        leading_num = re.match(r'^(\d+)', clean_qty)
        if leading_num:
            parsed_val = int(leading_num.group(1))
            if parsed_val > 0:
                return parsed_val, f"Explicit prescribed quantity: {clean_qty}"

    # 2. Extract terms for course calculation
    raw_dosage = getattr(med, 'dosage', None)
    raw_freq = getattr(med, 'frequency', None)
    raw_dur = getattr(med, 'duration', None)
    raw_inst = getattr(med, 'instructions', None)

    if isinstance(med, dict):
        raw_dosage = raw_dosage if raw_dosage is not None else med.get('dosage')
        raw_freq = raw_freq if raw_freq is not None else med.get('frequency')
        raw_dur = raw_dur if raw_dur is not None else med.get('duration')
        raw_inst = raw_inst if raw_inst is not None else med.get('instructions')

    dosage_str = str(raw_dosage or '').strip().lower()
    freq_str = str(raw_freq or '').strip().lower()
    dur_str = str(raw_dur or '').strip().lower()
    inst_str = str(raw_inst or '').strip().lower()

    # Medical Safety Check: PRN / SOS / as-needed
    combined_notes = f"{freq_str} {inst_str} {dosage_str}"
    if any(k in combined_notes for k in ['sos', 'prn', 'as needed', 'when needed', 'as required', 'if needed']):
        return None, "Quantity cannot be calculated for PRN/SOS (as-needed) administration."

    # Liquid Volume Check: liquid dosages (ml, tsp, tbsp, etc.) cannot be compared directly against discrete bottles/packs
    if re.search(r'\b(?:ml|milliliters?|tsp|tbsp|teaspoons?|tablespoons?)\b', dosage_str):
        return None, f"Liquid medicine volume '{raw_dosage}' cannot be directly converted to discrete inventory units."

    # Recover explicitly written schedule/duration if placed inside compound fields
    doses_per_day = _extract_frequency_doses(freq_str)
    if doses_per_day is None and not freq_str:
        doses_per_day = _extract_frequency_doses(dosage_str) or _extract_frequency_doses(inst_str)

    days = _extract_duration_days(dur_str)
    if days is None and not dur_str:
        days = (
            _extract_duration_days(freq_str)
            or _extract_duration_days(dosage_str)
            or _extract_duration_days(inst_str)
        )

    dose_per_intake = _parse_dose_per_intake(dosage_str)
    if dose_per_intake is None and not dosage_str:
        dose_per_intake = _parse_dose_per_intake(inst_str)

    if dose_per_intake is None and not dosage_str:
        missing = ["dosage"]
        if doses_per_day is None and not freq_str:
            missing.append("frequency")
        if days is None and not dur_str:
            missing.append("duration")
        return None, f"Required quantity not specified (missing: {', '.join(missing)})."

    if (not freq_str and doses_per_day is None) or (not dur_str and days is None):
        missing = []
        if not freq_str and doses_per_day is None:
            missing.append("frequency")
        if not dur_str and days is None:
            missing.append("duration")
        return None, f"Required quantity not specified (missing: {', '.join(missing)})."

    if dose_per_intake is None:
        return None, f"Dosage '{raw_dosage}' is qualitative or cannot be quantified per intake."

    if doses_per_day is None:
        return None, f"Frequency '{raw_freq}' is not a recognized deterministic schedule."

    if days is None:
        return None, f"Duration '{raw_dur}' is ongoing or cannot be quantified in days."

    raw_total = dose_per_intake * doses_per_day * days
    total_course = int(math.ceil(raw_total))
    dose_display = str(dose_per_intake.normalize())
    return total_course, (
        f"Calculated course: {dose_display} per intake x {doses_per_day} times/day x {days} days = {total_course} units."
    )


# ============================================================================
# 2. Strict Medicine Matching Engine
# ============================================================================

def _strength_signature(comps: List[Tuple[Decimal, str]]) -> Tuple[Tuple[str, str], ...]:
    """Produce canonical hashable signature for strength equivalence comparisons."""
    normalized: List[Tuple[str, str]] = []
    for val, unit in comps:
        if unit == 'g':
            normalized.append((str((val * Decimal('1000')).normalize()), 'mg'))
        elif unit == 'mcg' and val >= Decimal('1000') and (val % Decimal('1000')) == 0:
            normalized.append((str((val / Decimal('1000')).normalize()), 'mg'))
        else:
            normalized.append((str(val.normalize()), unit))
    return tuple(sorted(normalized))


def _name_tokens(text: str) -> List[str]:
    return [w for w in re.split(r'[^a-zA-Z0-9]+', (text or '').lower()) if w and not w.isdigit()]


def match_prescription_medicine_to_catalogue(med: Any) -> MedicineMatchResult:
    """
    Matches a prescribed medicine against the central Medicine catalogue
    using strict safety gates:
    - Rule A: Exact/normalized/spelling-verified brand or formulation matching preferred.
      Brand-to-brand substitution is strictly forbidden (e.g. Dolo != Crocin).
      Composition matching applies only when the prescribed name matches the sole active salt.
    - Rule B: Unit-aware strength compatibility required (500mg != 650mg; 50mcg != 50mg;
      single drug != multi-ingredient combination).
    - Rule C: Dosage form compatibility validated when specified (tablet vs syrup).
    - Rule D: Distinct conflicting formulations (different strengths or forms) return AMBIGUOUS;
      equivalent representations of the same formulation & strength group together cleanly.
    """
    raw_name = getattr(med, 'medicine_name', None)
    raw_strength = getattr(med, 'strength', None)
    raw_dosage = getattr(med, 'dosage', None)

    if isinstance(med, dict):
        raw_name = raw_name or med.get('medicine_name')
        raw_strength = raw_strength or med.get('strength')
        raw_dosage = raw_dosage or med.get('dosage')

    clean_name = str(raw_name or '').strip()
    if len(clean_name) < 2:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation="Prescribed medicine name is missing or too short."
        )

    base_name, effective_strength, _ = split_medicine_name_and_strength(clean_name, raw_strength)
    clean_dosage = str(raw_dosage or '').strip() if raw_dosage else None

    # Step 1: Query existing catalogue matcher candidates
    candidates = match_medicine_catalogue(medicine_name=clean_name, strength=effective_strength, limit=25)
    if not candidates:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation=f"No matching medicine found in master catalogue for '{clean_name}'."
        )

    norm_prescribed_full = normalize_token(clean_name)
    norm_prescribed_base = normalize_token(base_name)
    prescribed_brand_tokens = _name_tokens(base_name or clean_name)
    prescribed_strength_comps = parse_strength_components(effective_strength) or parse_strength_components(clean_name)
    prescribed_form_family = detect_dosage_form_family(clean_dosage) or detect_dosage_form_family(clean_name)

    filtered_direct: List[Dict[str, Any]] = []
    filtered_composition: List[Dict[str, Any]] = []

    for cand in candidates:
        cand_name = cand.get('medicine_name', '')
        cand_raw_str = cand.get('strength', '')
        cand_comp = cand.get('composition', '')
        cand_base, cand_eff_str, _ = split_medicine_name_and_strength(cand_name, cand_raw_str)
        cand_norm_name = normalize_token(cand_name)
        cand_norm_base = normalize_token(cand_base)
        cand_tokens = [w for w in re.split(r'[^a-zA-Z0-9]+', cand_name.lower()) if w]
        is_comp_match = cand.get('match_type', '').startswith('Composition Match')

        # Rule A: Identity & Brand Safety Gate
        if is_comp_match and norm_prescribed_base not in cand_norm_base and norm_prescribed_full not in cand_norm_name:
            # Allow generic-to-single-salt composition ONLY when sole active ingredient matches prescribed generic
            sole_salt = extract_single_ingredient_salt(cand_comp)
            if not sole_salt:
                continue
            salt_match = (
                normalize_token(sole_salt) == norm_prescribed_base
                or is_safe_spelling_match(base_name, sole_salt)
            )
            if not salt_match:
                continue
        else:
            if prescribed_brand_tokens:
                tokens_ok = all(bt in cand_tokens for bt in prescribed_brand_tokens)
                prefix_ok = (
                    len(prescribed_brand_tokens) == 1
                    and len(prescribed_brand_tokens[0]) >= 5
                    and any(t.startswith(prescribed_brand_tokens[0]) for t in cand_tokens)
                )
                spelling_ok = (
                    cand.get('match_type', '').startswith('Spelling Match')
                    and is_safe_spelling_match(base_name, cand_base)
                )
                if not (tokens_ok or prefix_ok or spelling_ok):
                    continue

        # Rule B: Strength Safety Gate
        cand_strength_comps = parse_strength_components(cand_raw_str or cand_eff_str or cand_name)
        if prescribed_strength_comps:
            if not strength_components_compatible(prescribed_strength_comps, cand_strength_comps):
                continue

        # Rule C: Dosage Form Safety Gate
        if prescribed_form_family:
            cand_form_text = f"{cand.get('dosage_form', '')} {cand_name}"
            cand_form_family = detect_dosage_form_family(cand_form_text)
            if cand_form_family and cand_form_family != prescribed_form_family:
                continue

        if is_comp_match and norm_prescribed_base != cand_norm_base:
            filtered_composition.append(cand)
        else:
            filtered_direct.append(cand)

    # Prefer direct formulation/generic matches; include single-salt equivalents alongside generic-name matches
    # (never alongside brand prescriptions, because brand names fail sole_salt equality above)
    filtered_candidates = filtered_direct + filtered_composition if filtered_direct else filtered_composition

    # Step 2: Evaluate filtered results
    if not filtered_candidates:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation=f"Catalogue medicines exist for '{clean_name}', but strength or dosage form did not match."
        )

    # Check if direct matches narrow down to one equivalent formulation signature
    direct_pool = filtered_direct or filtered_candidates

    # Prioritize exact formulation/base matches over broader prefix variants (e.g. 'Dolo 500' over 'Dolo 500 Extra')
    exact_matches = [
        c for c in direct_pool
        if normalize_token(c.get('medicine_name')) == norm_prescribed_full
        or normalize_token(split_medicine_name_and_strength(c.get('medicine_name'), c.get('strength'))[0]) == norm_prescribed_base
        or is_safe_spelling_match(base_name, split_medicine_name_and_strength(c.get('medicine_name'), c.get('strength'))[0])
    ]
    eval_pool = exact_matches if exact_matches else direct_pool

    # Group candidates by formulation equivalence: (normalized strength signature, dosage form family)
    # and check whether multiple conflicting strengths/forms exist
    distinct_strengths: Set[Tuple[Tuple[str, str], ...]] = set()
    distinct_forms: Set[str] = set()
    distinct_bases: Set[str] = set()

    for c in eval_pool:
        c_base, c_eff_str, _ = split_medicine_name_and_strength(c.get('medicine_name'), c.get('strength'))
        c_str_comps = parse_strength_components(c.get('strength') or c_eff_str or c.get('medicine_name'))
        c_form = detect_dosage_form_family(f"{c.get('dosage_form', '')} {c.get('medicine_name', '')}") or ''
        distinct_bases.add(normalize_token(c_base))
        if c_str_comps:
            distinct_strengths.add(_strength_signature(c_str_comps))
        if c_form:
            distinct_forms.add(c_form)

    has_conflicting_strengths = len(distinct_strengths) > 1
    has_conflicting_forms = len(distinct_forms) > 1 and not prescribed_form_family
    has_conflicting_brands = len(distinct_bases) > 1 and not exact_matches

    if has_conflicting_strengths or has_conflicting_forms or has_conflicting_brands:
        sample_names = list(dict.fromkeys(c.get('medicine_name', '') for c in eval_pool))[:3]
        return MedicineMatchResult(
            match_status="AMBIGUOUS",
            explanation=(
                f"Multiple plausible catalogue formulations found for '{clean_name}' "
                f"({', '.join(sample_names)}). Please specify strength or dosage form."
            )
        )

    # All candidates in final_pool (plus single-salt equivalents matching the same strength) are safe equivalents
    equivalence_ids = [c['id'] for c in eval_pool]
    for c in filtered_composition:
        if c['id'] not in equivalence_ids:
            c_base, c_eff_str, _ = split_medicine_name_and_strength(c.get('medicine_name'), c.get('strength'))
            c_str_comps = parse_strength_components(c.get('strength') or c_eff_str or c.get('medicine_name'))
            if not prescribed_strength_comps or strength_components_compatible(prescribed_strength_comps, c_str_comps):
                equivalence_ids.append(c['id'])

    meds_by_id = Medicine.objects.in_bulk(equivalence_ids)
    matched_meds = [meds_by_id[mid] for mid in equivalence_ids if mid in meds_by_id]

    if not matched_meds:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation=f"Could not verify catalogue medicine for '{clean_name}'."
        )

    primary_med = matched_meds[0]
    if normalize_token(primary_med.medicine_name) == norm_prescribed_full:
        explanation = f"Exact verified match: '{primary_med.medicine_name}'."
    elif clean_name.lower() != primary_med.medicine_name.lower():
        explanation = f"Matched formulation '{primary_med.medicine_name}' for '{clean_name}'."
    else:
        explanation = f"Verified formulation match: '{primary_med.medicine_name}'."

    return MedicineMatchResult(
        medicine=primary_med,
        match_status="MATCHED",
        explanation=explanation,
        matched_medicines=matched_meds,
    )


# ============================================================================
# 3. Pharmacy Stock Retrieval & Expiry Gate
# ============================================================================

def get_pharmacy_medicine_stock(pharmacy: PharmacyProfile, medicine: Medicine) -> int:
    """
    Calculates total non-expired, available stock for a medicine at a specific pharmacy.
    Strictly excludes:
    - Batches where expiry_date < today
    - Batches where quantity <= 0
    Returns 0 if no valid inventory batches exist.
    """
    if not pharmacy or not medicine or not pharmacy.pk or not medicine.pk:
        return 0

    today = timezone.localdate()
    stock_sum = (
        Inventory.objects.filter(
            pharmacy=pharmacy,
            medicine=medicine,
            expiry_date__gte=today,
            quantity__gt=0,
        ).aggregate(total=Sum('quantity'))['total']
        or 0
    )
    return int(stock_sum)


# ============================================================================
# 4. Prescription-to-Pharmacy Availability Engine
# ============================================================================

def check_prescription_pharmacy_availability(
    prescription: Prescription,
    patient_lat: float,
    patient_lon: float,
    radius_km: float = 10.0
) -> List[PharmacyAvailabilityReport]:
    """
    Compares all confirmed medicines of a prescription against nearby approved pharmacies.

    Workflow:
    1. Validates prescription and retrieves all confirmed medicine items.
    2. Identifies approved registered pharmacies with valid coordinates within radius_km.
    3. Matches each prescribed medicine against the master catalogue once (collecting
       all verified equivalent catalogue records for the same formulation + strength).
    4. Aggregates non-expired inventory batches across matching pharmacies and equivalent
       catalogue IDs in a single query.
    5. Evaluates item-level and overall pharmacy availability statuses.
    6. Returns structured, nearest-first pharmacy availability reports.
    """
    if not prescription or not hasattr(prescription, 'medicines'):
        return []

    confirmed_meds = list(prescription.medicines.all().order_by('id'))
    if not confirmed_meds:
        return []

    # 1. Fetch approved registered pharmacies with valid coordinates
    try:
        p_lat = float(patient_lat)
        p_lon = float(patient_lon)
        rad_km = float(radius_km)
    except (TypeError, ValueError):
        return []

    approved_pharmacies = list(
        PharmacyProfile.objects.filter(
            approval_status='APPROVED',
            latitude__isnull=False,
            longitude__isnull=False,
        ).select_related('user')
    )

    # 2. Filter within search radius using Haversine distance
    nearby_pharmacies: List[Tuple[PharmacyProfile, float]] = []
    for pharma in approved_pharmacies:
        try:
            pharma_lat = float(pharma.latitude)
            pharma_lon = float(pharma.longitude)
        except (TypeError, ValueError):
            continue

        dist = calculate_haversine_distance(p_lat, p_lon, pharma_lat, pharma_lon)
        if dist <= rad_km:
            nearby_pharmacies.append((pharma, dist))

    # Sort nearest first
    nearby_pharmacies.sort(key=lambda x: x[1])
    if not nearby_pharmacies:
        return []

    # 3. Match each confirmed prescription medicine against the catalogue (computed once)
    medicine_matches: Dict[int, MedicineMatchResult] = {}
    quantity_info: Dict[int, Tuple[Optional[int], str]] = {}
    all_matched_med_ids: Set[int] = set()

    for med in confirmed_meds:
        match_res = match_prescription_medicine_to_catalogue(med)
        qty_val, qty_note = parse_quantity_or_calculate_course(med)

        medicine_matches[med.id] = match_res
        quantity_info[med.id] = (qty_val, qty_note)

        if match_res.match_status == 'MATCHED':
            equivs = match_res.matched_medicines or ([match_res.medicine] if match_res.medicine else [])
            for eq in equivs:
                all_matched_med_ids.add(eq.id)

    # 4. Bulk query inventory for all matched equivalent medicines across nearby pharmacies
    today = timezone.localdate()
    stock_map: Dict[Tuple[int, int], int] = {}
    carried_pairs: Set[Tuple[int, int]] = set()

    if all_matched_med_ids:
        pharmacy_ids = [pharma.id for pharma, _ in nearby_pharmacies]
        inv_rows = (
            Inventory.objects.filter(
                pharmacy_id__in=pharmacy_ids,
                medicine_id__in=all_matched_med_ids,
                expiry_date__gte=today,
                quantity__gt=0,
            )
            .values('pharmacy_id', 'medicine_id')
            .annotate(total_qty=Sum('quantity'))
        )
        for row in inv_rows:
            stock_map[(row['pharmacy_id'], row['medicine_id'])] = int(row['total_qty'] or 0)

        carried_pairs = set(
            Inventory.objects.filter(
                pharmacy_id__in=pharmacy_ids,
                medicine_id__in=all_matched_med_ids,
            ).values_list('pharmacy_id', 'medicine_id')
        )

    # 5. Build pharmacy-wise availability reports
    reports: List[PharmacyAvailabilityReport] = []
    total_prescribed = len(confirmed_meds)

    for pharma, dist_km in nearby_pharmacies:
        items: List[MedicineAvailabilityItem] = []
        available_items_count = 0
        fully_available_count = 0

        for med in confirmed_meds:
            m_res = medicine_matches[med.id]
            req_qty, req_note = quantity_info[med.id]

            common_rx_fields = dict(
                prescribed_dosage=med.dosage,
                prescribed_frequency=med.frequency,
                prescribed_duration=med.duration,
                prescribed_quantity=med.quantity,
                prescribed_instructions=med.instructions,
            )

            if m_res.match_status == 'AMBIGUOUS':
                items.append(MedicineAvailabilityItem(
                    prescription_medicine_id=med.id,
                    prescribed_name=med.medicine_name,
                    prescribed_strength=med.strength,
                    matched_catalogue_id=None,
                    matched_catalogue_name=None,
                    match_status='AMBIGUOUS',
                    required_quantity=req_qty,
                    required_quantity_note=req_note,
                    available_quantity=0,
                    availability_status='AMBIGUOUS',
                    explanation=m_res.explanation,
                    **common_rx_fields,
                ))
            elif m_res.match_status == 'UNMAPPED':
                items.append(MedicineAvailabilityItem(
                    prescription_medicine_id=med.id,
                    prescribed_name=med.medicine_name,
                    prescribed_strength=med.strength,
                    matched_catalogue_id=None,
                    matched_catalogue_name=None,
                    match_status='UNMAPPED',
                    required_quantity=req_qty,
                    required_quantity_note=req_note,
                    available_quantity=0,
                    availability_status='NOT_STOCKED',
                    explanation=m_res.explanation,
                    **common_rx_fields,
                ))
            else:  # MATCHED
                equiv_meds = m_res.matched_medicines or ([m_res.medicine] if m_res.medicine else [])
                # Aggregate active stock across verified equivalent formulations at this pharmacy
                avail_qty = 0
                best_stocked_med = m_res.medicine
                best_stocked_qty = -1
                has_any_batch_record = False

                for eq_med in equiv_meds:
                    eq_qty = stock_map.get((pharma.id, eq_med.id), 0)
                    avail_qty += eq_qty
                    if (pharma.id, eq_med.id) in carried_pairs:
                        has_any_batch_record = True
                    if eq_qty > best_stocked_qty:
                        best_stocked_qty = eq_qty
                        best_stocked_med = eq_med

                matched_med = best_stocked_med if (avail_qty > 0 and best_stocked_med) else m_res.medicine

                if req_qty is not None:
                    if avail_qty >= req_qty:
                        avail_status = 'FULLY_AVAILABLE'
                        explanation = f"In stock: {avail_qty} units available (Required: {req_qty})."
                        fully_available_count += 1
                        available_items_count += 1
                    elif avail_qty > 0:
                        avail_status = 'PARTIALLY_AVAILABLE'
                        explanation = f"Partial stock: {avail_qty} units available (Required: {req_qty})."
                        available_items_count += 1
                    else:
                        avail_status = 'OUT_OF_STOCK'
                        if has_any_batch_record:
                            explanation = "Currently out of stock at this pharmacy (0 units available)."
                        else:
                            explanation = "Not stocked at this pharmacy (0 units available)."
                else:
                    if avail_qty > 0:
                        avail_status = 'IN_STOCK'
                        explanation = f"In stock: {avail_qty} units available ({req_note})"
                        available_items_count += 1
                    else:
                        avail_status = 'OUT_OF_STOCK'
                        if has_any_batch_record:
                            explanation = "Currently out of stock at this pharmacy (0 units available)."
                        else:
                            explanation = "Not stocked at this pharmacy (0 units available)."

                items.append(MedicineAvailabilityItem(
                    prescription_medicine_id=med.id,
                    prescribed_name=med.medicine_name,
                    prescribed_strength=med.strength,
                    matched_catalogue_id=matched_med.id if matched_med else None,
                    matched_catalogue_name=matched_med.medicine_name if matched_med else None,
                    match_status='MATCHED',
                    required_quantity=req_qty,
                    required_quantity_note=req_note,
                    available_quantity=avail_qty,
                    availability_status=avail_status,
                    explanation=explanation,
                    **common_rx_fields,
                ))

        # Overall pharmacy availability status determination
        if fully_available_count == total_prescribed:
            overall_status = 'FULLY_AVAILABLE'
        elif available_items_count > 0:
            overall_status = 'PARTIALLY_AVAILABLE'
        else:
            overall_status = 'OUT_OF_STOCK'

        reports.append(PharmacyAvailabilityReport(
            pharmacy_id=pharma.id,
            pharmacy_name=pharma.pharmacy_name,
            phone_number=pharma.phone_number or '',
            address=pharma.address or '',
            distance_km=dist_km,
            latitude=float(pharma.latitude),
            longitude=float(pharma.longitude),
            total_prescribed_count=total_prescribed,
            available_items_count=available_items_count,
            overall_status=overall_status,
            items=items,
        ))

    return reports
