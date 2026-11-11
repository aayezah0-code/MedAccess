"""
Catalogue Formulation Matcher for MedAccess Pharmacy.

Searches the existing Master Medicine Catalogue (7,400+ formulation records)
against voice-extracted and prescription medicine names and strengths.
Implements normalization, candidate generation, spelling-similarity fallback,
and multi-tier ranking without modifying or creating catalogue records.
"""

from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import re
from typing import List, Dict, Any, Optional, Tuple
from django.db.models import Q
from pharmacy.models import Medicine


DOSAGE_PREFIX_RE = re.compile(
    r'^(?:tab(?:let)?s?|cap(?:sule)?s?|syr(?:up)?|susp(?:ension)?|inj(?:ection)?|drop(?:s)?|oint(?:ment)?|cream|gel)\b\.?\s*',
    re.IGNORECASE,
)

FORM_SUFFIX_WORDS = {
    'tablet', 'tablets', 'tab', 'tabs', 'capsule', 'capsules', 'cap', 'caps',
    'syrup', 'suspension', 'liquid', 'solution', 'elixir', 'drops', 'drop',
    'injection', 'inj', 'vial', 'ampoule', 'inhaler', 'respules', 'rotacap',
    'rotacaps', 'cream', 'ointment', 'gel', 'lotion', 'er', 'sr', 'xr', 'cr',
    'dr', 'dt', 'md', 'od', 'ip',
}

STRENGTH_TOKEN_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(mcg|µg|ug|mg|gm|g|iu/ml|iu|ml|%|w/v|w/w)\b',
    re.IGNORECASE,
)

# Parenthesized strength fragments like "(50 mcg)" or "(40mg + 5mg)"
PAREN_STRENGTH_RE = re.compile(
    r'\(\s*\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|gm|g|iu/ml|iu|ml|%|w/v|w/w)'
    r'(?:\s*[+/]\s*\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|gm|g|iu/ml|iu|ml|%|w/v|w/w))*\s*\)',
    re.IGNORECASE,
)

MULTI_INGREDIENT_COMPOSITION_RE = re.compile(r'\)\s*[+/]\s*')


def normalize_token(text: Optional[str]) -> str:
    """Normalize text for consistent scoring comparison."""
    if not text:
        return ""
    return re.sub(r'[\s\-_/().]+', '', str(text)).lower()


def normalize_unit(unit: Optional[str]) -> str:
    """Normalize strength unit aliases into canonical lowercase forms."""
    u = (unit or '').strip().lower()
    if u in ('µg', 'ug', 'mcg'):
        return 'mcg'
    if u == 'gm':
        return 'g'
    return u


def parse_strength_components(text: Optional[str]) -> List[Tuple[Decimal, str]]:
    """
    Parse ordered (magnitude, unit) pairs from a strength or medicine string.
    Normalizes mass units (g -> mg -> mcg comparison ready via scale helper)
    while retaining unitless numbers only when no explicit unit token exists.
    """
    if not text:
        return []
    raw = str(text)
    unit_matches = STRENGTH_TOKEN_RE.findall(raw)
    components: List[Tuple[Decimal, str]] = []
    if unit_matches:
        for num_str, unit_str in unit_matches:
            try:
                val = Decimal(num_str).normalize()
            except InvalidOperation:
                continue
            components.append((val, normalize_unit(unit_str)))
        return components

    # Fallback: unitless standalone numbers (e.g. strength='650' or 'Dolo 650')
    for num_str in re.findall(r'\b(\d+(?:\.\d+)?)\b', raw):
        try:
            components.append((Decimal(num_str).normalize(), ''))
        except InvalidOperation:
            continue
    return components


def _to_base_mass_mcg(val: Decimal, unit: str) -> Optional[Decimal]:
    if unit == 'g':
        return val * Decimal('1000000')
    if unit == 'mg':
        return val * Decimal('1000')
    if unit == 'mcg':
        return val
    return None


def single_strength_equal(a: Tuple[Decimal, str], b: Tuple[Decimal, str]) -> bool:
    """Compare two strength components with safe unit awareness."""
    val_a, unit_a = a
    val_b, unit_b = b

    if unit_a and unit_b:
        mass_a = _to_base_mass_mcg(val_a, unit_a)
        mass_b = _to_base_mass_mcg(val_b, unit_b)
        if mass_a is not None and mass_b is not None:
            return mass_a == mass_b
        return unit_a == unit_b and val_a == val_b

    # One side omitted unit (e.g. '650' vs '650mg')
    return val_a == val_b


def strength_components_compatible(
    prescribed: List[Tuple[Decimal, str]],
    candidate: List[Tuple[Decimal, str]],
) -> bool:
    """
    Determine whether candidate strength strictly satisfies prescribed strength.
    Rejects multi-ingredient combination products when single strength is prescribed.
    """
    if not prescribed:
        return True
    if not candidate:
        return False

    if len(prescribed) == 1:
        if len(candidate) > 1:
            return False
        return single_strength_equal(prescribed[0], candidate[0])

    if len(candidate) != len(prescribed):
        return False

    remaining = list(candidate)
    for p_comp in prescribed:
        matched_idx = None
        for idx, c_comp in enumerate(remaining):
            if single_strength_equal(p_comp, c_comp):
                matched_idx = idx
                break
        if matched_idx is None:
            return False
        remaining.pop(matched_idx)
    return True


def split_medicine_name_and_strength(
    medicine_name: Optional[str],
    strength: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    Extract cleaned drug name (without dosage prefixes, trailing form suffixes,
    or embedded strengths), effective strength string, and cleaned full name.
    Example: 'Tab. Levothyroxine (50 mcg)' -> ('Levothyroxine', '50 mcg', 'Levothyroxine (50 mcg)')
    """
    clean_full = ' '.join(str(medicine_name or '').strip().split())
    clean_strength = ' '.join(str(strength or '').strip().split())

    without_prefix = DOSAGE_PREFIX_RE.sub('', clean_full).strip()
    without_parens = PAREN_STRENGTH_RE.sub('', without_prefix).strip()

    # Extract embedded strength tokens if caller did not provide strength
    embedded_strengths = STRENGTH_TOKEN_RE.findall(without_prefix)
    if not clean_strength and embedded_strengths:
        clean_strength = ' + '.join(f"{num} {unit}" for num, unit in embedded_strengths)

    # Strip embedded strength + trailing form tokens to get base drug name
    stripped = STRENGTH_TOKEN_RE.sub('', without_parens).strip()
    tokens = [t for t in re.split(r'\s+', stripped) if t]
    base_tokens: List[str] = []
    for tok in tokens:
        low = tok.strip('.,()-').lower()
        if low in FORM_SUFFIX_WORDS:
            continue
        if not clean_strength and low.isdigit():
            clean_strength = low
            continue
        base_tokens.append(tok.strip(','))

    base_name = ' '.join(base_tokens).strip(' -/()')
    return (base_name or without_prefix or clean_full), clean_strength, clean_full


def extract_single_ingredient_salt(composition: Optional[str]) -> Optional[str]:
    """
    If composition describes a single active ingredient (e.g. 'Telmisartan (40mg)'),
    return the normalized salt name; return None for multi-ingredient combinations.
    """
    if not composition:
        return None
    comp = str(composition).strip()
    if not comp or '+' in comp or MULTI_INGREDIENT_COMPOSITION_RE.search(comp):
        return None
    salt_only = re.sub(r'\([^)]*\)', '', comp).strip()
    salt_only = STRENGTH_TOKEN_RE.sub('', salt_only).strip()
    return salt_only or None


def is_safe_spelling_match(query_base: str, candidate_base: str, min_ratio: float = 0.84) -> bool:
    """
    Determine whether query_base is a high-confidence minor spelling/OCR match
    for candidate_base (e.g. 'Telismartan' -> 'Telmisartan', 'Asprin' -> 'Aspirin').
    Guards short words, token-count mismatches, and unrelated prefixes
    (e.g. 'Cetirizine' != 'Levocetirizine', 'Telismartan' != 'Telsartan R').
    """
    q_words = [w for w in re.findall(r'[a-z]+', (query_base or '').lower()) if w]
    c_words = [w for w in re.findall(r'[a-z]+', (candidate_base or '').lower()) if w]
    if not q_words or len(q_words) != len(c_words):
        return False

    q_norm = ''.join(q_words)
    c_norm = ''.join(c_words)
    if len(q_norm) < 5 or len(c_norm) < 5:
        return False
    same_prefix2 = q_norm[:2] == c_norm[:2]
    transposed_2_3 = q_norm[0] == c_norm[0] and sorted(q_norm[1:3]) == sorted(c_norm[1:3])
    if not (same_prefix2 or transposed_2_3):
        return False
    if abs(len(q_norm) - len(c_norm)) > 2:
        return False
    return SequenceMatcher(None, q_norm, c_norm).ratio() >= min_ratio


def match_medicine_catalogue(
    medicine_name: Optional[str],
    strength: Optional[str] = None,
    limit: int = 6
) -> List[Dict[str, Any]]:
    """
    Queries the existing Medicine catalogue for matches against medicine_name and strength.
    Uses multi-tiered query selection + unit/punctuation normalization + safe spelling
    similarity fallback so exact, normalized, and single-salt matches are retrieved cleanly.
    """
    if not medicine_name or len(medicine_name.strip()) < 2:
        return []

    base_name, effective_strength, clean_name = split_medicine_name_and_strength(medicine_name, strength)
    norm_name = normalize_token(clean_name)
    norm_base = normalize_token(base_name)
    norm_strength = normalize_token(effective_strength) if effective_strength else ""
    prescribed_strength_comps = parse_strength_components(effective_strength)

    # Query pool larger than display limit so multi-ingredient drugs never crowd out exact singles
    pool_limit = max(limit * 4, 25)
    seen_ids = set()
    combined_meds: List[Medicine] = []

    def _add_meds(qs):
        for m in qs:
            if m.id not in seen_ids:
                seen_ids.add(m.id)
                combined_meds.append(m)

    search_terms = []
    for term in (clean_name, base_name):
        t = (term or '').strip()
        if t and t.lower() not in {x.lower() for x in search_terms}:
            search_terms.append(t)
    # Also try spacing inserted between digits and unit (e.g. 'Levothyroxine 50mcg' -> 'Levothyroxine 50 mcg')
    spaced_name = re.sub(r'(\d)([a-zA-Z]+)', r'\1 \2', clean_name).strip()
    if spaced_name.lower() not in {x.lower() for x in search_terms}:
        search_terms.append(spaced_name)

    for term in search_terms:
        _add_meds(Medicine.objects.filter(medicine_name__iexact=term)[:pool_limit])
        _add_meds(
            Medicine.objects.filter(medicine_name__istartswith=term)
            .exclude(id__in=seen_ids)[:pool_limit]
        )

    for term in search_terms:
        _add_meds(
            Medicine.objects.filter(medicine_name__icontains=term)
            .exclude(id__in=seen_ids)[:pool_limit]
        )

    # Also match single-word drug base in composition cleanly
    comp_term = base_name if len(base_name) >= 4 else clean_name
    _add_meds(
        Medicine.objects.filter(composition__icontains=comp_term)
        .exclude(id__in=seen_ids)[:pool_limit]
    )

    # Fallback: bounded spelling/OCR similarity search when direct queries return nothing
    base_alpha = re.sub(r'[^a-zA-Z]', '', base_name)
    if not combined_meds and len(base_alpha) >= 5:
        first_char = base_alpha[0]
        fuzzy_rows = Medicine.objects.filter(
            Q(medicine_name__istartswith=first_char) | Q(composition__istartswith=first_char)
        ).values_list('id', 'medicine_name', 'strength', 'composition')
        matched_fuzzy_ids = []
        for mid, mname, mstr, mcomp in fuzzy_rows:
            med_base, _, _ = split_medicine_name_and_strength(mname, mstr)
            salt_base = extract_single_ingredient_salt(mcomp)
            if is_safe_spelling_match(base_name, med_base) or (
                salt_base and is_safe_spelling_match(base_name, salt_base)
            ):
                matched_fuzzy_ids.append(mid)
        if matched_fuzzy_ids:
            _add_meds(Medicine.objects.filter(id__in=matched_fuzzy_ids[:pool_limit * 2]))

    candidates = []
    for med in combined_meds:
        med_norm_name = normalize_token(med.medicine_name)
        med_base, med_eff_str, _ = split_medicine_name_and_strength(med.medicine_name, med.strength)
        med_norm_base = normalize_token(med_base)
        med_norm_comp = normalize_token(med.composition)
        med_norm_str = normalize_token(med.strength or med_eff_str)
        med_strength_comps = parse_strength_components(med.strength or med_eff_str)

        score = 0
        match_type = "Partial Match"
        confidence = "low"

        # 1. Exact full name match
        if med_norm_name == norm_name or (norm_base and med_norm_name == norm_base):
            score += 110
            match_type = "Exact Match"
            confidence = "high"
        # 2. Exact base drug name match (e.g. 'Levothyroxine (50 mcg)' vs 'Levothyroxine 50 mcg')
        elif norm_base and med_norm_base == norm_base:
            score += 95
            match_type = "Exact Match"
            confidence = "high"
        # 3. Name prefix match
        elif med_norm_name.startswith(norm_name) or (norm_base and med_norm_name.startswith(norm_base)):
            score += 80
            match_type = "Prefix Match"
            confidence = "high"
        # 4. Contained inside medicine name
        elif norm_name in med_norm_name or (norm_base and len(norm_base) >= 4 and norm_base in med_norm_name):
            score += 50
            match_type = "Name Match"
            confidence = "medium"
        # 5. Salt / composition match
        elif (norm_name and norm_name in med_norm_comp) or (norm_base and len(norm_base) >= 4 and norm_base in med_norm_comp):
            score += 35
            match_type = "Composition Match"
            confidence = "medium"
        # 6. Safe spelling similarity match (e.g. 'Telismartan' -> 'Telmisartan')
        else:
            salt_base = extract_single_ingredient_salt(med.composition)
            if is_safe_spelling_match(base_name, med_base):
                score += 75
                match_type = "Spelling Match"
                confidence = "high"
            elif salt_base and is_safe_spelling_match(base_name, salt_base):
                score += 35
                match_type = "Composition Match"
                confidence = "medium"

        # Strength bonus / compatibility boost
        strength_matched = False
        if prescribed_strength_comps:
            if strength_components_compatible(prescribed_strength_comps, med_strength_comps):
                score += 35
                strength_matched = True
            elif norm_strength and (norm_strength in med_norm_str or norm_strength in med_norm_name):
                score += 20
                strength_matched = True
        elif norm_strength and (norm_strength in med_norm_str or norm_strength in med_norm_name):
            score += 30
            strength_matched = True

        if strength_matched:
            match_type += " + Strength Match"
            if confidence == "medium":
                confidence = "high"

        # Prefer single-ingredient formulations when a single-ingredient strength was searched
        if prescribed_strength_comps and len(prescribed_strength_comps) == 1 and len(med_strength_comps) == 1:
            score += 10

        candidates.append({
            'id': med.id,
            'medicine_name': med.medicine_name,
            'strength': med.strength or '',
            'composition': med.composition or '',
            'manufacturer': med.manufacturer or '',
            'dosage_form': med.dosage_form or '',
            'pack_size': med.pack_size or '',
            'mrp': float(med.mrp) if med.mrp is not None else None,
            'match_type': match_type,
            'confidence': confidence,
            'score': score,
        })

    # Sort candidates by score descending, then medicine_name ascending
    candidates.sort(key=lambda c: (-c['score'], c['medicine_name']))

    # Return top N candidates without the internal score field
    top_candidates = candidates[:limit]
    for c in top_candidates:
        c.pop('score', None)

    return top_candidates
