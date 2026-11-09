"""
Catalogue Formulation Matcher for MedAccess Pharmacy.

Searches the existing Master Medicine Catalogue (7,400+ formulation records)
against voice-extracted medicine names and strengths.
Implements ranking, candidate generation, and match classification.
Zero database modifications or record creation.
"""

from typing import List, Dict, Any, Optional
from django.db.models import Q
from pharmacy.models import Medicine


def normalize_token(text: Optional[str]) -> str:
    """Normalize text for consistent scoring comparison."""
    if not text:
        return ""
    import re
    return re.sub(r'[\s\-_/]+', '', text).lower()


def match_medicine_catalogue(
    medicine_name: Optional[str],
    strength: Optional[str] = None,
    limit: int = 6
) -> List[Dict[str, Any]]:
    """
    Queries the existing Medicine catalogue for matches against medicine_name and strength.
    Uses multi-tiered query selection so that exact and prefix medicine_name matches
    are guaranteed to be retrieved and prioritized over composition matches.
    
    Ranking priority:
    1. Exact medicine_name match (+ strength bonus)
    2. Starts-with medicine_name match (+ strength bonus)
    3. Contains medicine_name match
    4. Composition (salt) matches
    """
    if not medicine_name or len(medicine_name.strip()) < 2:
        return []

    clean_name = medicine_name.strip()
    norm_name = normalize_token(clean_name)
    norm_strength = normalize_token(strength) if strength else ""

    # Tier 1: Exact medicine name
    exact_qs = list(Medicine.objects.filter(medicine_name__iexact=clean_name)[:limit])
    seen_ids = {m.id for m in exact_qs}

    # Tier 2: Prefix medicine name (e.g. "Paracetamol 500mg Tablet", "Crocin Advance Tablet")
    prefix_qs = list(
        Medicine.objects.filter(medicine_name__istartswith=clean_name)
        .exclude(id__in=seen_ids)[:limit]
    )
    seen_ids.update(m.id for m in prefix_qs)

    # Tier 3: Contains in medicine name
    name_contains_qs = list(
        Medicine.objects.filter(medicine_name__icontains=clean_name)
        .exclude(id__in=seen_ids)[:limit * 2]
    )
    seen_ids.update(m.id for m in name_contains_qs)

    # Tier 4: Contains in composition
    comp_qs = list(
        Medicine.objects.filter(composition__icontains=clean_name)
        .exclude(id__in=seen_ids)[:limit * 2]
    )
    seen_ids.update(m.id for m in comp_qs)

    combined_meds = exact_qs + prefix_qs + name_contains_qs + comp_qs

    candidates = []
    for med in combined_meds:
        med_norm_name = normalize_token(med.medicine_name)
        med_norm_comp = normalize_token(med.composition)
        med_norm_str = normalize_token(med.strength)

        score = 0
        match_type = "Partial Match"
        confidence = "low"

        # 1. Exact name match
        if med_norm_name == norm_name:
            score += 100
            match_type = "Exact Match"
            confidence = "high"
        # 2. Name prefix match (starts with extracted name)
        elif med_norm_name.startswith(norm_name):
            score += 80
            match_type = "Prefix Match"
            confidence = "high"
        # 3. Extracted name contained inside medicine name
        elif norm_name in med_norm_name:
            score += 50
            match_type = "Name Match"
            confidence = "medium"
        # 4. Salt / composition match
        elif norm_name in med_norm_comp:
            score += 35
            match_type = "Composition Match"
            confidence = "medium"

        # Strength bonus
        if norm_strength:
            if norm_strength in med_norm_str or norm_strength in med_norm_name:
                score += 30
                match_type += " + Strength Match"
                if confidence == "medium":
                    confidence = "high"

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
