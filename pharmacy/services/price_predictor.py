import os
import re
import math
import logging
from typing import Dict, Any, Optional, Union
import numpy as np
import scipy.sparse
import joblib
from django.conf import settings

logger = logging.getLogger(__name__)

# Module-level singleton cache for the loaded v2 model bundle
_V2_MODEL_BUNDLE: Optional[Dict[str, Any]] = None

V2_MODEL_RELATIVE_PATH = os.path.join('ml_models', 'price_prediction_model_v2.pkl')


def get_model_bundle() -> Dict[str, Any]:
    """
    Lazy-loads and caches the v2 Ridge Regression model bundle into memory.
    Ensures the 2.1 MB pickle is deserialized only once rather than on every HTTP request.
    Strictly loads price_prediction_model_v2.pkl and never touches v1.
    """
    global _V2_MODEL_BUNDLE
    if _V2_MODEL_BUNDLE is None:
        model_path = os.path.join(settings.BASE_DIR, V2_MODEL_RELATIVE_PATH)
        if not os.path.exists(model_path):
            logger.error(f"Price prediction model v2 not found at: {model_path}")
            raise FileNotFoundError(f"Model file not found: {V2_MODEL_RELATIVE_PATH}")

        logger.info(f"Loading Medicine Price Prediction Model v2 from {model_path}...")
        try:
            bundle = joblib.load(model_path)
            # Validate bundle integrity
            required_keys = {'model', 'encoder', 'scaler', 'features', 'categorical_features', 'numerical_features'}
            missing = required_keys - set(bundle.keys())
            if missing:
                raise ValueError(f"Corrupt v2 model bundle. Missing keys: {missing}")

            _V2_MODEL_BUNDLE = bundle
            logger.info("Medicine Price Prediction Model v2 successfully loaded and cached.")
        except Exception as e:
            logger.exception(f"Failed to load price prediction model v2: {e}")
            raise RuntimeError(f"Unable to load price prediction model: {str(e)}")

    return _V2_MODEL_BUNDLE


def clear_model_cache():
    """Clears the cached model bundle (used in tests or reloading)."""
    global _V2_MODEL_BUNDLE
    _V2_MODEL_BUNDLE = None


def extract_pack_size_from_quantity(quantity_str: str) -> Optional[float]:
    """
    Attempts to extract the first numeric pack size / quantity from a string like:
    'strip of 10 tablets' -> 10.0
    'bottle of 60 ml Solution' -> 60.0
    """
    if not quantity_str:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)', str(quantity_str))
    if m:
        try:
            val = float(m.group(1))
            return val if val > 0 else None
        except ValueError:
            return None
    return None


def predict_medicine_price(
    name: str,
    quantity: Optional[str] = None,
    manufacturer: Optional[str] = None,
    salt_composition: Optional[str] = None,
    pack_size: Optional[Union[int, float, str]] = None
) -> Dict[str, Any]:
    """
    Predicts estimated Maximum Retail Price (MRP in INR) using the trained
    Ridge Regression Model v2.

    Parameters:
        name (str): Medicine brand name (required)
        quantity (str): Packaging description (e.g. 'strip of 10 tablets')
        manufacturer (str): Pharma company name (optional)
        salt_composition (str): Active chemical formulation (optional)
        pack_size (float/int/str): Numeric units count (required or extracted from quantity)

    Returns:
        dict containing:
            'success': bool
            'estimated_mrp': float (if success)
            'currency': 'INR'
            'pack_size': float
            'name': str
            'quantity': str
            'manufacturer': str
            'salt_composition': str
            'model_name': str
            'error': str (if failed)
    """
    # 1. Validation: Name
    clean_name = (name or '').strip()
    if not clean_name:
        return {
            'success': False,
            'error': 'Medicine Name is required for price prediction.',
            'field': 'name'
        }

    # 2. Validation: Pack Size
    parsed_pack_size = None
    if pack_size is not None and str(pack_size).strip() != '':
        try:
            val = float(pack_size)
            if math.isnan(val) or math.isinf(val) or val <= 0:
                return {
                    'success': False,
                    'error': 'Pack Size must be a positive number greater than 0.',
                    'field': 'pack_size'
                }
            parsed_pack_size = val
        except (ValueError, TypeError):
            return {
                'success': False,
                'error': f"Invalid pack size '{pack_size}'. Please enter a valid number.",
                'field': 'pack_size'
            }
    elif quantity:
        parsed_pack_size = extract_pack_size_from_quantity(quantity)

    if parsed_pack_size is None or parsed_pack_size <= 0:
        return {
            'success': False,
            'error': 'A valid numeric Pack Size (> 0) is required.',
            'field': 'pack_size'
        }

    # 3. Clean Categorical Features
    clean_qty = (quantity or '').strip()
    if not clean_qty:
        clean_qty = f"pack of {int(parsed_pack_size) if parsed_pack_size.is_integer() else parsed_pack_size} units"

    clean_mfg = (manufacturer or '').strip() or 'Unknown'
    clean_salt = (salt_composition or '').strip() or 'Unknown'

    # 4. Load Model
    try:
        bundle = get_model_bundle()
        model = bundle['model']
        encoder = bundle['encoder']
        scaler = bundle['scaler']
        min_bound = bundle.get('min_mrp', 1.0)
        max_bound = bundle.get('max_mrp', 5000.0)
        model_name = bundle.get('model_name', 'Ridge Regression (L2 Regularized v2)')
    except Exception as e:
        logger.error(f"Error accessing price prediction model: {e}")
        return {
            'success': False,
            'error': 'Price prediction model service is temporarily unavailable.'
        }

    # 5. Preprocessing & Encoding Sequence
    try:
        # Categorical features in exact order: ['Name', 'Quantity', 'Manufacturer', 'Salt_Composition']
        cat_input = [[clean_name, clean_qty, clean_mfg, clean_salt]]
        X_cat = encoder.transform(cat_input)

        # Numerical feature: ['Pack_Size'] scaled with training StandardScaler
        num_input = np.array([[parsed_pack_size]], dtype=np.float64)
        X_num = scaler.transform(num_input)

        # Combine sparse matrix horizontally
        X_final = scipy.sparse.hstack([X_cat, X_num])
    except Exception as e:
        logger.exception(f"Preprocessing error during price prediction: {e}")
        return {
            'success': False,
            'error': f"Preprocessing failed: {str(e)}"
        }

    # 6. Inference
    try:
        raw_pred = float(model.predict(X_final)[0])
    except Exception as e:
        logger.exception(f"Model prediction error: {e}")
        return {
            'success': False,
            'error': f"Model execution failed: {str(e)}"
        }

    # 7. Finite / NaN Sanity Checks
    if math.isnan(raw_pred) or math.isinf(raw_pred):
        logger.error(f"Non-finite prediction encountered: {raw_pred}")
        return {
            'success': False,
            'error': 'Model generated an invalid or non-finite price estimate.'
        }

    # Enforce non-negative boundary (clamped to realistic minimum of Rs 1.00 and ceiling Rs 5000.00)
    final_mrp = max(float(min_bound), min(float(raw_pred), float(max_bound)))
    final_mrp = round(final_mrp, 2)

    return {
        'success': True,
        'estimated_mrp': final_mrp,
        'raw_prediction': round(raw_pred, 2),
        'currency': 'INR',
        'pack_size': parsed_pack_size,
        'name': clean_name,
        'quantity': clean_qty,
        'manufacturer': clean_mfg,
        'salt_composition': clean_salt,
        'model_name': model_name,
        'is_bound_clamped': (raw_pred < min_bound or raw_pred > max_bound),
    }

