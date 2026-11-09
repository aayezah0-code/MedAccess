"""
Medicine Uses & Side Effects Predictor
Part of MedAccess Intelligent Clinical Assistance Modules.

Matches a user-entered medicine name against the curated Medicine_Details database
using Character N-Gram TF-IDF Vectorization and Cosine Similarity.
"""

import os
import re
import sys
import json
import joblib
import numpy as np

# Directory constants
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(CURRENT_DIR, "side_effects_model_bundle.pkl")

# Module singleton cache
_MODEL_BUNDLE = None


def normalize_text(text: str) -> str:
    """Standardizes input string for consistent n-gram tokenization."""
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = re.sub(r'[^\w\s\-\.\%\/]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def get_model_bundle():
    """
    Lazy-loads and caches the model bundle into memory.
    Deserializes only once across multiple invocations.
    """
    global _MODEL_BUNDLE
    if _MODEL_BUNDLE is None:
        if not os.path.exists(BUNDLE_PATH):
            raise FileNotFoundError(
                f"Model bundle not found at {BUNDLE_PATH}. "
                "Ensure build_and_save_model.py has been executed."
            )
        _MODEL_BUNDLE = joblib.load(BUNDLE_PATH)
    return _MODEL_BUNDLE


def predict_medicine_details(query: str, top_k: int = 5) -> dict:
    """
    Matches a user query to medicine uses and side effects.

    Parameters:
        query (str): User-entered medicine name (e.g. 'Dolo 650', 'Augmentin')
        top_k (int): Number of top candidate recommendations to return

    Returns:
        dict containing:
            'query': raw query string
            'normalized_query': preprocessed query
            'similarity_score': top match cosine similarity score (0.0 to 1.0)
            'match_status': 'HIGH_CONFIDENCE_MATCH' | 'MEDIUM_CONFIDENCE_MATCH' | 'UNCERTAIN_OR_NO_MATCH'
            'best_match': dict of top medicine details or None if uncertain
            'top_candidates': list of top-k candidates with scores
            'disclaimer': safety and clinical reference notice
    """
    raw_query = str(query or "").strip()
    norm_query = normalize_text(raw_query)

    if not norm_query or len(norm_query) < 2:
        return {
            'query': raw_query,
            'normalized_query': norm_query,
            'similarity_score': 0.0,
            'match_status': 'EMPTY_OR_TOO_SHORT',
            'best_match': None,
            'top_candidates': [],
            'message': 'Please provide a medicine name with at least 2 characters.',
            'disclaimer': 'Clinical information is for informational reference only.'
        }

    bundle = get_model_bundle()
    vectorizer = bundle['vectorizer']
    tfidf_matrix = bundle['tfidf_matrix']
    records = bundle['records']
    config = bundle.get('config', {})

    high_thresh = config.get('high_confidence_threshold', 0.75)
    med_thresh = config.get('medium_confidence_threshold', 0.50)

    # 1. Transform query
    q_vec = vectorizer.transform([norm_query])

    # 2. Compute Cosine Similarities via sparse matrix multiplication
    sims = (q_vec @ tfidf_matrix.T).toarray().ravel()
    top_indices = np.argsort(-sims)[:top_k]

    # 3. Format candidate list
    top_candidates = []
    for rank_idx, idx in enumerate(top_indices, start=1):
        rec = records[idx]
        score = round(float(sims[idx]), 4)
        top_candidates.append({
            'rank': rank_idx,
            'medicine_name': rec['medicine_name'],
            'similarity_score': score,
            'composition': rec['composition'],
            'manufacturer': rec['manufacturer'],
            'uses_cleaned': rec['uses_cleaned'],
            'uses_list': rec['uses_list'],
            'uses_raw': rec['uses_raw'],
            'side_effects_merged': rec['side_effects_merged'],
            'side_effects_raw': rec['side_effects_raw'],
            'image_url': rec['image_url']
        })

    best_cand = top_candidates[0] if top_candidates else None
    top_score = best_cand['similarity_score'] if best_cand else 0.0

    # 4. Apply Confidence Classification
    if top_score >= high_thresh:
        status = 'HIGH_CONFIDENCE_MATCH'
    elif top_score >= med_thresh:
        status = 'MEDIUM_CONFIDENCE_MATCH'
    else:
        status = 'UNCERTAIN_OR_NO_MATCH'

    return {
        'query': raw_query,
        'normalized_query': norm_query,
        'similarity_score': top_score,
        'match_status': status,
        'best_match': best_cand if status != 'UNCERTAIN_OR_NO_MATCH' else None,
        'top_candidates': top_candidates,
        'thresholds': {
            'high_confidence': high_thresh,
            'medium_confidence': med_thresh
        },
        'disclaimer': (
            "Clinical uses and side effects reference based on Medicine_Details dataset. "
            "Similarity score represents text alignment and is not a medical diagnostic probability. "
            "Always consult a licensed medical professional before administering medication."
        )
    }


def main():
    """Command-line interface for testing medicine predictions."""
    if len(sys.argv) > 1:
        query_input = " ".join(sys.argv[1:])
    else:
        query_input = "Augmentin 625"

    print("=" * 70)
    print(f"MedAccess Medicine Uses & Side Effects Matcher")
    print("=" * 70)
    print(f"Query: '{query_input}'\n")

    result = predict_medicine_details(query_input, top_k=3)

    print(f"Match Status:     {result['match_status']}")
    print(f"Similarity Score: {result['similarity_score']:.4f}")
    print("-" * 70)

    if result['best_match']:
        match = result['best_match']
        print(f"Medicine Name:    {match['medicine_name']}")
        print(f"Composition:      {match['composition']}")
        print(f"Manufacturer:     {match['manufacturer']}")
        print(f"\nClinical Uses:")
        for u in match['uses_list']:
            print(f"  • {u}")
        print(f"\nSide Effects:")
        print(f"  {match['side_effects_merged']}")
    else:
        print("No reliable match found above the confidence threshold.")
        print("Candidate suggestions:")
        for cand in result['top_candidates']:
            print(f"  - {cand['medicine_name']} (Similarity: {cand['similarity_score']:.4f})")

    print("-" * 70)
    print(f"Disclaimer: {result['disclaimer']}")
    print("=" * 70)


if __name__ == "__main__":
    main()

