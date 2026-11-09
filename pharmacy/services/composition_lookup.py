import logging
from typing import Dict, Any, List, Optional
from rapidfuzz import process, fuzz
from pharmacy.models import MedicineComposition

logger = logging.getLogger(__name__)

# Module-level cache for unique names to provide sub-second fuzzy matching
_DISTINCT_NAMES_CACHE: Optional[List[str]] = None


def get_distinct_medicine_names() -> List[str]:
    """
    Returns a cached list of unique medicine names from MedicineComposition.
    Lazy loaded once to optimize RapidFuzz matching across requests.
    """
    global _DISTINCT_NAMES_CACHE
    if _DISTINCT_NAMES_CACHE is None:
        try:
            _DISTINCT_NAMES_CACHE = list(
                MedicineComposition.objects.values_list('name', flat=True).distinct()
            )
        except Exception as e:
            logger.error(f"Error loading distinct medicine names for cache: {e}")
            _DISTINCT_NAMES_CACHE = []
    return _DISTINCT_NAMES_CACHE


def clear_distinct_names_cache():
    """Clears the cached medicine names (used in tests or after re-import)."""
    global _DISTINCT_NAMES_CACHE
    _DISTINCT_NAMES_CACHE = None


def lookup_medicine_composition(query: str, disambiguate_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Look up verified medicine composition following the strict priority rules:
    1. If disambiguate_id is provided, return that exact record as verified.
    2. Exact case-insensitive match on medicine name:
       - If exactly 1 unique salt composition: return VERIFIED_MATCH
       - If >1 unique salt compositions: return AMBIGUOUS_COMPOSITION with candidate options
    3. If no exact match:
       - Use RapidFuzz to find suggestions with match score >= 70%
       - Never auto-return unverified fuzzy results as verified (return status SUGGESTIONS)
    4. If no suggestions meet the threshold:
       - Return status NOT_FOUND
    """
    clean_query = (query or '').strip()
    if not clean_query and not disambiguate_id:
        return {
            'status': 'NOT_FOUND',
            'query': '',
            'message': 'Please enter a medicine name to search.',
            'is_verified': False,
            'products': [],
            'candidates': [],
            'suggestions': []
        }

    # 1. Direct record selection via disambiguate_id
    if disambiguate_id:
        try:
            record = MedicineComposition.objects.get(pk=disambiguate_id)
            return {
                'status': 'VERIFIED_MATCH',
                'query': clean_query or record.name,
                'message': f"Verified composition for '{record.name}'.",
                'is_verified': True,
                'match_type': 'EXACT_DISAMBIGUATED',
                'medicine_name': record.name,
                'salt_composition': record.salt_composition,
                'manufacturer': record.manufacturer or 'N/A',
                'quantity': record.quantity or 'N/A',
                'mrp': float(record.mrp) if record.mrp is not None else None,
                'products': [{
                    'id': record.id,
                    'name': record.name,
                    'manufacturer': record.manufacturer or 'N/A',
                    'quantity': record.quantity or 'N/A',
                    'mrp': float(record.mrp) if record.mrp is not None else None,
                    'salt_composition': record.salt_composition
                }],
                'candidates': [],
                'suggestions': []
            }
        except MedicineComposition.DoesNotExist:
            pass

    # 2. Exact match (case-insensitive)
    exact_qs = MedicineComposition.objects.filter(name__iexact=clean_query)
    if exact_qs.exists():
        records = list(exact_qs)
        # Extract unique salt compositions
        unique_salts = {}
        for r in records:
            normalized_salt = r.salt_composition.strip()
            if normalized_salt not in unique_salts:
                unique_salts[normalized_salt] = []
            unique_salts[normalized_salt].append(r)

        if len(unique_salts) == 1:
            first_record = records[0]
            salt_comp = list(unique_salts.keys())[0]
            products = [
                {
                    'id': r.id,
                    'name': r.name,
                    'manufacturer': r.manufacturer or 'N/A',
                    'quantity': r.quantity or 'N/A',
                    'mrp': float(r.mrp) if r.mrp is not None else None,
                    'salt_composition': r.salt_composition
                }
                for r in records
            ]
            return {
                'status': 'VERIFIED_MATCH',
                'query': clean_query,
                'message': f"Verified composition found for '{first_record.name}'.",
                'is_verified': True,
                'match_type': 'EXACT',
                'medicine_name': first_record.name,
                'salt_composition': salt_comp,
                'manufacturer': first_record.manufacturer or 'N/A',
                'quantity': first_record.quantity or 'N/A',
                'mrp': float(first_record.mrp) if first_record.mrp is not None else None,
                'products': products,
                'candidates': [],
                'suggestions': []
            }
        else:
            # Multiple distinct formulations for the exact same name -> Disambiguation required
            candidates = [
                {
                    'id': r.id,
                    'name': r.name,
                    'manufacturer': r.manufacturer or 'Not Specified',
                    'quantity': r.quantity or 'Standard Pack',
                    'mrp': float(r.mrp) if r.mrp is not None else None,
                    'salt_composition': r.salt_composition
                }
                for r in records
            ]
            return {
                'status': 'AMBIGUOUS_COMPOSITION',
                'query': clean_query,
                'message': f"Multiple distinct formulations found for '{clean_query}'. Please select the specific variant or manufacturer below to verify.",
                'is_verified': False,
                'medicine_name': clean_query,
                'candidates': candidates,
                'products': [],
                'suggestions': []
            }

    # 3. No exact match -> RapidFuzz Suggestions (>= 70% threshold)
    # Fast path: first check database for partial matches (icontains or istartswith)
    candidate_names = []
    # Token-based database pre-filter for speed
    tokens = [t for t in clean_query.split() if len(t) >= 3]
    if tokens:
        from django.db.models import Q
        token_q = Q(name__icontains=clean_query)
        for t in tokens[:2]:
            token_q |= Q(name__icontains=t)
        db_candidates = list(
            MedicineComposition.objects.filter(token_q)
            .values_list('name', flat=True)
            .distinct()[:200]
        )
        candidate_names.extend(db_candidates)

    # If DB pre-filter produced fewer than 10 candidates, use the cached distinct names
    if len(candidate_names) < 10:
        candidate_names = get_distinct_medicine_names()

    if candidate_names:
        fuzzy_results = process.extract(
            clean_query,
            candidate_names,
            scorer=fuzz.WRatio,
            score_cutoff=70.0,
            limit=5
        )

        if fuzzy_results:
            suggestions = [
                {
                    'name': res[0],
                    'score': round(float(res[1]), 1)
                }
                for res in fuzzy_results
            ]
            return {
                'status': 'SUGGESTIONS',
                'query': clean_query,
                'message': f"No exact verified match for '{clean_query}'. Did you mean one of these medicines?",
                'is_verified': False,
                'suggestions': suggestions,
                'products': [],
                'candidates': []
            }

    # 4. No matches meeting >= 70%
    return {
        'status': 'NOT_FOUND',
        'query': clean_query,
        'message': f"No verified medicine composition found for '{clean_query}'. Please verify the spelling or check the medicine brand name.",
        'is_verified': False,
        'products': [],
        'candidates': [],
        'suggestions': []
    }

