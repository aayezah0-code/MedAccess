"""
MedAccess Inventory Availability Engine (Phase 1).

Compares a patient's confirmed prescription medicines against the active,
non-expired inventory of nearby APPROVED MedAccess pharmacies.

Key Capabilities:
1. parse_quantity_or_calculate_course: Safely parses explicit quantities or
   computes course totals from dosage, frequency, and duration without guessing.
2. match_prescription_medicine_to_catalogue: Strict multi-tier matching against
   the master Medicine catalogue with strength and dosage-form safety gates.
   Never silently substitutes different brands or unvetted compositions.
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
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Sum
from django.utils import timezone

from patient.models import Prescription, PrescriptionMedicine
from pharmacy.models import Medicine, Inventory, PharmacyProfile
from pharmacy.services.catalogue_matcher import match_medicine_catalogue, normalize_token

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

def parse_quantity_or_calculate_course(med: Any) -> Tuple[Optional[int], str]:
    """
    Determines required medicine quantity from a prescription medicine record.

    Priority:
    1. Explicit prescribed quantity when present (e.g. '10 tablets', '1 bottle', '30').
    2. Computed course: dosage x frequency x duration when all three are unambiguous.
       Example: 1 tablet x 2 times/day x 5 days = 10 units.
    3. Returns (None, explanation) for SOS/PRN, qualitative instructions, or missing terms.
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
        raw_dosage = raw_dosage or med.get('dosage')
        raw_freq = raw_freq or med.get('frequency')
        raw_dur = raw_dur or med.get('duration')
        raw_inst = raw_inst or med.get('instructions')

    if not raw_dosage or not raw_freq or not raw_dur:
        missing = []
        if not raw_dosage:
            missing.append("dosage")
        if not raw_freq:
            missing.append("frequency")
        if not raw_dur:
            missing.append("duration")
        return None, f"Required quantity not specified (missing: {', '.join(missing)})."

    dosage_str = str(raw_dosage).strip().lower()
    freq_str = str(raw_freq).strip().lower()
    dur_str = str(raw_dur).strip().lower()
    inst_str = str(raw_inst or '').strip().lower()

    # Medical Safety Check: PRN / SOS / as-needed
    combined_notes = f"{freq_str} {inst_str} {dosage_str}"
    if any(k in combined_notes for k in ['sos', 'prn', 'as needed', 'when needed', 'as required', 'if needed']):
        return None, "Quantity cannot be calculated for PRN/SOS (as-needed) administration."

    # Liquid Volume Check: liquid dosages (ml, tsp, tbsp, etc.) cannot be compared directly against discrete bottles/packs
    if re.search(r'\b(?:ml|milliliters?|tsp|tbsp|teaspoons?|tablespoons?)\b', dosage_str):
        return None, f"Liquid medicine volume '{raw_dosage}' cannot be directly converted to discrete inventory units."

    # Parse dosage count per intake (e.g. '1 tablet', '2 capsules', '1 tab', '2')
    dose_match = re.search(r'(\d+)\s*(?:tablet|tab|capsule|cap|pill|puff|drop)?s?\b', dosage_str)
    if not dose_match:
        return None, f"Dosage '{raw_dosage}' is qualitative or cannot be quantified per intake."

    dose_per_intake = int(dose_match.group(1))
    if dose_per_intake <= 0:
        return None, f"Dosage per intake must be positive, got {dose_per_intake}."

    # Parse frequency to doses per day
    # Pattern A: 1-0-1, 1-1-1, 1-0-0, 0-0-1, 0-1-0, 1-1-1-1, 1-0-1-0
    pattern_num = re.match(r'^(\d+)\s*[-–]\s*(\d+)(?:\s*[-–]\s*(\d+))?(?:\s*[-–]\s*(\d+))?$', freq_str)
    if pattern_num:
        parts = [int(p) for p in pattern_num.groups() if p is not None]
        doses_per_day = sum(parts)
    elif freq_str in ['once daily', 'once a day', 'od', 'qd', 'every 24 hours', 'q24h']:
        doses_per_day = 1
    elif freq_str in ['twice daily', 'twice a day', 'bd', 'bid', 'every 12 hours', 'q12h']:
        doses_per_day = 2
    elif freq_str in ['thrice daily', 'three times daily', 'three times a day', 'tds', 'tid', 'every 8 hours', 'q8h']:
        doses_per_day = 3
    elif freq_str in ['four times daily', 'four times a day', 'qid', 'every 6 hours', 'q6h']:
        doses_per_day = 4
    else:
        times_match = re.search(r'(\d+)\s*times?\s*(?:a|per)?\s*day', freq_str)
        if times_match:
            doses_per_day = int(times_match.group(1))
        else:
            return None, f"Frequency '{raw_freq}' is not a recognized deterministic schedule."

    if doses_per_day <= 0:
        return None, f"Doses per day must be positive, got {doses_per_day}."

    # Parse duration in days
    days = None
    day_match = re.search(r'(\d+)\s*days?', dur_str)
    if day_match:
        days = int(day_match.group(1))
    else:
        week_match = re.search(r'(\d+)\s*weeks?', dur_str)
        if week_match:
            days = int(week_match.group(1)) * 7
        else:
            month_match = re.search(r'(\d+)\s*months?', dur_str)
            if month_match:
                days = int(month_match.group(1)) * 30

    if not days or days <= 0:
        return None, f"Duration '{raw_dur}' is ongoing or cannot be quantified in days."

    total_course = dose_per_intake * doses_per_day * days
    return total_course, f"Calculated course: {dose_per_intake} per intake x {doses_per_day} times/day x {days} days = {total_course} units."


# ============================================================================
# 2. Strict Medicine Matching Engine
# ============================================================================

def match_prescription_medicine_to_catalogue(med: Any) -> MedicineMatchResult:
    """
    Matches a prescribed medicine against the central Medicine catalogue
    using strict safety gates:
    - Rule A: Exact brand/formulation matching preferred. Composition matching
      alone NEVER authorizes a brand substitute.
    - Rule B: Strength compatibility required. Reject different strengths (e.g. 500mg vs 650mg).
    - Rule C: Dosage form compatibility validated when specified (tablet vs syrup).
    - Rule D: Ambiguous or competing candidates return AMBIGUOUS; never pick arbitrarily.
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

    clean_strength = str(raw_strength or '').strip() if raw_strength else None
    clean_dosage = str(raw_dosage or '').strip() if raw_dosage else None

    # Step 1: Query existing catalogue matcher candidates
    candidates = match_medicine_catalogue(medicine_name=clean_name, strength=clean_strength, limit=10)
    if not candidates:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation=f"No matching medicine found in master catalogue for '{clean_name}'."
        )

    norm_prescribed_name = normalize_token(clean_name)
    prescribed_name_tokens = set(re.findall(r'[a-zA-Z0-9]+', clean_name.lower()))
    prescribed_brand_tokens = [w for w in re.split(r'[^a-zA-Z0-9]+', clean_name.lower()) if w and not w.isdigit()]
    prescribed_strength_nums = extract_numeric_tokens(clean_strength) or extract_numeric_tokens(clean_name)
    prescribed_form_family = detect_dosage_form_family(clean_dosage) or detect_dosage_form_family(clean_name)

    filtered_candidates: List[Dict[str, Any]] = []

    for cand in candidates:
        cand_name = cand.get('medicine_name', '')
        cand_norm_name = normalize_token(cand_name)
        cand_tokens = [w for w in re.split(r'[^a-zA-Z0-9]+', cand_name.lower()) if w]

        # Rule A: Identity & Brand Safety Gate
        # Reject candidates matched purely on composition if brand name is not in the candidate title
        if cand.get('match_type') == 'Composition Match' and norm_prescribed_name not in cand_norm_name:
            continue

        # Candidate must contain the prescribed brand word tokens as distinct tokens
        # E.g. 'dolo' in 'Dolo 500 Tablet' -> Match; but NOT in 'Dolospas' or 'Dologel'
        if prescribed_brand_tokens:
            if not all(bt in cand_tokens for bt in prescribed_brand_tokens):
                # Also allow prefix match only if prescribed is at least 4 chars and starts the first token
                if not any(t.startswith(prescribed_brand_tokens[0]) for t in cand_tokens if len(prescribed_brand_tokens[0]) >= 5):
                    continue

        # Rule B: Strength Safety Gate
        if prescribed_strength_nums:
            cand_strength_text = f"{cand.get('strength', '')} {cand_name}"
            cand_strength_nums = extract_numeric_tokens(cand_strength_text)
            # If candidate specifies numbers, at least one of prescribed numbers must match
            if not cand_strength_nums or not (set(prescribed_strength_nums) & set(cand_strength_nums)):
                continue

        # Rule C: Dosage Form Safety Gate
        if prescribed_form_family:
            cand_form_text = f"{cand.get('dosage_form', '')} {cand_name}"
            cand_form_family = detect_dosage_form_family(cand_form_text)
            if cand_form_family and cand_form_family != prescribed_form_family:
                continue

        filtered_candidates.append(cand)

    # Step 2: Evaluate filtered results
    if not filtered_candidates:
        return MedicineMatchResult(
            match_status="UNMAPPED",
            explanation=f"Catalogue medicines exist for '{clean_name}', but strength or dosage form did not match."
        )

    # Check for exact name match among filtered candidates
    exact_name_matches = [
        c for c in filtered_candidates
        if normalize_token(c.get('medicine_name')) == norm_prescribed_name
    ]
    if len(exact_name_matches) == 1:
        med_id = exact_name_matches[0]['id']
        catalogue_med = Medicine.objects.filter(id=med_id).first()
        if catalogue_med:
            return MedicineMatchResult(
                medicine=catalogue_med,
                match_status="MATCHED",
                explanation=f"Exact verified match: '{catalogue_med.medicine_name}'."
            )

    # If single clear candidate remains
    if len(filtered_candidates) == 1:
        med_id = filtered_candidates[0]['id']
        catalogue_med = Medicine.objects.filter(id=med_id).first()
        if catalogue_med:
            return MedicineMatchResult(
                medicine=catalogue_med,
                match_status="MATCHED",
                explanation=f"Verified formulation match: '{catalogue_med.medicine_name}'."
            )

    # Multiple candidates: check if they are identical formulations or conflicting
    distinct_names = {c.get('medicine_name') for c in filtered_candidates}
    distinct_strengths = {c.get('strength') for c in filtered_candidates}
    distinct_forms = {c.get('dosage_form') for c in filtered_candidates}

    if len(distinct_names) > 1 or len(distinct_strengths) > 1 or len(distinct_forms) > 1:
        sample_names = list(distinct_names)[:3]
        return MedicineMatchResult(
            match_status="AMBIGUOUS",
            explanation=f"Multiple plausible catalogue formulations found for '{clean_name}' ({', '.join(sample_names)}). Prescription specification is ambiguous."
        )

    # If all remaining candidates share identical name, strength, and form, pick the canonical one
    first_med = Medicine.objects.filter(id=filtered_candidates[0]['id']).first()
    if first_med:
        return MedicineMatchResult(
            medicine=first_med,
            match_status="MATCHED",
            explanation=f"Verified catalogue match: '{first_med.medicine_name}'."
        )

    return MedicineMatchResult(
        match_status="UNMAPPED",
        explanation=f"Could not verify catalogue medicine for '{clean_name}'."
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
    3. Matches each prescribed medicine against the master catalogue once.
    4. Aggregates inventory batches across all matching pharmacies and medicines in a single query.
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
    matched_med_ids: List[int] = []

    for med in confirmed_meds:
        match_res = match_prescription_medicine_to_catalogue(med)
        qty_val, qty_note = parse_quantity_or_calculate_course(med)

        medicine_matches[med.id] = match_res
        quantity_info[med.id] = (qty_val, qty_note)

        if match_res.match_status == 'MATCHED' and match_res.medicine:
            matched_med_ids.append(match_res.medicine.id)

    # 4. Bulk query inventory for all matched medicines across nearby pharmacies
    today = timezone.localdate()
    stock_map: Dict[Tuple[int, int], int] = {}

    if matched_med_ids:
        pharmacy_ids = [pharma.id for pharma, _ in nearby_pharmacies]
        inv_rows = (
            Inventory.objects.filter(
                pharmacy_id__in=pharmacy_ids,
                medicine_id__in=matched_med_ids,
                expiry_date__gte=today,
                quantity__gt=0,
            )
            .values('pharmacy_id', 'medicine_id')
            .annotate(total_qty=Sum('quantity'))
        )
        for row in inv_rows:
            stock_map[(row['pharmacy_id'], row['medicine_id'])] = int(row['total_qty'] or 0)

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
                ))
            else:  # MATCHED
                matched_med = m_res.medicine
                avail_qty = stock_map.get((pharma.id, matched_med.id), 0)

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
                        explanation = f"Currently out of stock at this pharmacy (0 units available)."
                else:
                    if avail_qty > 0:
                        avail_status = 'IN_STOCK'
                        explanation = f"In stock: {avail_qty} units available (Course duration/quantity not specified)."
                        available_items_count += 1
                    else:
                        avail_status = 'OUT_OF_STOCK'
                        explanation = f"Currently out of stock at this pharmacy (0 units available)."

                items.append(MedicineAvailabilityItem(
                    prescription_medicine_id=med.id,
                    prescribed_name=med.medicine_name,
                    prescribed_strength=med.strength,
                    matched_catalogue_id=matched_med.id,
                    matched_catalogue_name=matched_med.medicine_name,
                    match_status='MATCHED',
                    required_quantity=req_qty,
                    required_quantity_note=req_note,
                    available_quantity=avail_qty,
                    availability_status=avail_status,
                    explanation=explanation,
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
