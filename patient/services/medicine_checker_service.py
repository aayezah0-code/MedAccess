"""
Medicine Checker Service
Encapsulates ML-based matching of user medicine queries to curated clinical
Uses, Composition, and Side Effects from the Medicine_Details database.
"""

import logging
from typing import Dict, Any, Optional
from ml_models.medicine_side_effects_model.predict_medicine import (
    predict_medicine_details,
    get_model_bundle,
    normalize_text,
)

logger = logging.getLogger(__name__)

# Fast in-memory name lookup map
_EXACT_NAME_INDEX: Optional[Dict[str, Dict[str, Any]]] = None


def _get_exact_name_index() -> Dict[str, Dict[str, Any]]:
    """Builds and caches a case-insensitive lookup table for confirmed matches."""
    global _EXACT_NAME_INDEX
    if _EXACT_NAME_INDEX is None:
        bundle = get_model_bundle()
        records = bundle.get('records', [])
        index = {}
        for rec in records:
            key = rec['medicine_name'].strip().lower()
            if key not in index:
                index[key] = rec
        _EXACT_NAME_INDEX = index
    return _EXACT_NAME_INDEX


def get_medicine_by_exact_name(medicine_name: str) -> Optional[Dict[str, Any]]:
    """
    Direct O(1) lookup for a confirmed medicine name.
    Used when a patient confirms a medium-confidence candidate.
    """
    if not medicine_name:
        return None
    key = str(medicine_name).strip().lower()
    index = _get_exact_name_index()
    rec = index.get(key)
    if not rec:
        return None

    return {
        'medicine_name': rec['medicine_name'],
        'composition': rec['composition'],
        'manufacturer': rec['manufacturer'],
        'uses_cleaned': rec['uses_cleaned'],
        'uses_list': rec['uses_list'],
        'uses_raw': rec['uses_raw'],
        'side_effects_merged': rec['side_effects_merged'],
        'side_effects_raw': rec['side_effects_raw'],
        'image_url': rec['image_url'],
    }


def get_medicine_autocomplete_suggestions(query: str, limit: int = 7) -> list:
    """
    Lightweight prefix & token search across canonical medicine names.
    Returns at most `limit` suggestions (default 7) containing:
      - 'medicine_name': canonical title
      - 'composition': active formulation
    Strictly does NOT include uses or side effects in autocomplete.
    """
    if not query:
        return []

    q_norm = normalize_text(str(query).strip())
    if len(q_norm) < 2:
        return []

    bundle = get_model_bundle()
    records = bundle.get('records', [])
    q_tokens = q_norm.split()

    tier1_full_prefix = []
    tier2_word_prefix = []
    tier3_substring = []
    seen = set()

    for rec in records:
        name_norm = rec['medicine_name_normalized']
        raw_name = rec['medicine_name']

        if raw_name in seen:
            continue

        # Tier 1: Full name prefix match
        if name_norm.startswith(q_norm):
            seen.add(raw_name)
            tier1_full_prefix.append({
                'medicine_name': raw_name,
                'composition': rec.get('composition', '')
            })
            if len(tier1_full_prefix) >= limit:
                break
            continue

        # Tier 2: Every query token prefixes a word in the medicine name
        name_words = name_norm.split()
        all_tokens_start_word = True
        matched_word_indices = set()
        for qt in q_tokens:
            token_matched = False
            for w_idx, nw in enumerate(name_words):
                if w_idx not in matched_word_indices and nw.startswith(qt):
                    token_matched = True
                    matched_word_indices.add(w_idx)
                    break
            if not token_matched:
                all_tokens_start_word = False
                break

        if all_tokens_start_word:
            seen.add(raw_name)
            tier2_word_prefix.append({
                'medicine_name': raw_name,
                'composition': rec.get('composition', '')
            })
            if len(tier1_full_prefix) + len(tier2_word_prefix) >= limit:
                continue

        # Tier 3: All query tokens appear as substrings
        elif all(qt in name_norm for qt in q_tokens):
            seen.add(raw_name)
            tier3_substring.append({
                'medicine_name': raw_name,
                'composition': rec.get('composition', '')
            })

    results = (tier1_full_prefix + tier2_word_prefix + tier3_substring)[:limit]
    return results


def check_medicine_uses_and_side_effects(query: str, top_k: int = 5) -> Dict[str, Any]:
    """
    Matches a user query to medicine uses and side effects.

    Returns:
        dict with:
            'success': bool
            'query': str
            'match_status': 'HIGH' | 'MEDIUM' | 'NO_MATCH' | 'EMPTY_OR_TOO_SHORT'
            'similarity_score': float
            'best_match': dict or None
            'candidates': list of candidate dicts
            'disclaimer': str
    """
    clean_query = str(query or "").strip()
    if not clean_query or len(clean_query) < 2:
        return {
            'success': False,
            'query': clean_query,
            'match_status': 'EMPTY_OR_TOO_SHORT',
            'message': 'Please enter a medicine name with at least 2 characters.',
            'best_match': None,
            'candidates': [],
            'disclaimer': 'Clinical information is for informational reference only.'
        }

    try:
        raw_result = predict_medicine_details(clean_query, top_k=top_k)
    except Exception as e:
        logger.exception(f"Error during medicine matching for query '{clean_query}': {e}")
        return {
            'success': False,
            'query': clean_query,
            'match_status': 'ERROR',
            'error': 'The medicine checker service encountered an internal error. Please try again.',
            'best_match': None,
            'candidates': [],
            'disclaimer': 'Clinical information is for informational reference only.'
        }

    status_code = raw_result.get('match_status')
    score = raw_result.get('similarity_score', 0.0)
    top_candidates = raw_result.get('top_candidates', [])
    best_cand = raw_result.get('best_match')

    # Map model status to service domain status
    if status_code == 'HIGH_CONFIDENCE_MATCH':
        domain_status = 'HIGH'
    elif status_code == 'MEDIUM_CONFIDENCE_MATCH':
        domain_status = 'MEDIUM'
    else:
        domain_status = 'NO_MATCH'

    return {
        'success': True,
        'query': clean_query,
        'normalized_query': raw_result.get('normalized_query', ''),
        'match_status': domain_status,
        'similarity_score': score,
        # Only expose best_match for HIGH confidence.
        # MEDIUM confidence requires patient confirmation before revealing details.
        'best_match': best_cand if domain_status == 'HIGH' else None,
        'suggested_confirmation': top_candidates[0] if (domain_status == 'MEDIUM' and top_candidates) else None,
        'candidates': top_candidates,
        'thresholds': raw_result.get('thresholds', {'high_confidence': 0.75, 'medium_confidence': 0.50}),
        'disclaimer': raw_result.get('disclaimer', '')
    }

