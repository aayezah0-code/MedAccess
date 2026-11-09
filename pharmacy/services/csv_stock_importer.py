"""
CSV Bulk Stock Importer & Validation Service for MedAccess Pharmacy.

Supports flexible CSV formats:
- Variable column names with synonym/alias recognition
- Variable column orders and extra ignored columns
- UTF-8 and UTF-8 with BOM (utf-8-sig)
- Multiple delimiters (comma, semicolon, tab)
- Multi-format expiry dates (DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, MM/YYYY, etc.)
- In-file and database batch deduplication
- Automatic column mapping detection with manual override
- Catalogue matching with multiple candidate resolution
- Row-level transactional import and error report generation
"""

import csv
import io
import re
import calendar
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Tuple, Any, Optional

from django.db import transaction
from django.db.models import Q
from pharmacy.models import Medicine, Inventory


# Maintainable synonym mappings for flexible column detection
COLUMN_ALIASES = {
    'medicine_name': [
        'medicine_name', 'medicine', 'product_name', 'product',
        'drug_name', 'item_name', 'name', 'med_name', 'brand_name',
        'item', 'drug', 'medicine_title', 'formulation'
    ],
    'strength': [
        'strength', 'medicine_strength', 'dosage_strength', 'potency',
        'dose', 'mg', 'ml', 'concentration'
    ],
    'batch_number': [
        'batch_number', 'batch_no', 'batch', 'lot_number', 'lot_no',
        'lot', 'batch_id', 'lot_id', 'b_no', 'batch_num'
    ],
    'expiry_date': [
        'expiry_date', 'expiry', 'exp_date', 'expiration_date',
        'exp', 'expiry_dt', 'expire_date', 'valid_till', 'exp_dt'
    ],
    'quantity': [
        'quantity', 'qty', 'stock', 'available_stock', 'current_stock',
        'stock_qty', 'units_count', 'count', 'qty_available', 'inventory_qty'
    ],
    'unit': [
        'unit', 'stock_unit', 'quantity_unit', 'packaging_unit',
        'pack_unit', 'uom', 'measure_unit', 'package'
    ],
    'low_stock_limit': [
        'low_stock_limit', 'minimum_stock', 'reorder_level', 'min_stock',
        'low_stock', 'alert_limit', 'min_limit', 'threshold'
    ],
    'composition': [
        'composition', 'salt_composition', 'salt', 'ingredients', 'active_ingredients'
    ],
    'manufacturer': [
        'manufacturer', 'company', 'brand_owner', 'manufactured_by'
    ],
    'dosage_form': [
        'dosage_form', 'form', 'medicine_type', 'product_form'
    ],
    'pack_size': [
        'pack_size', 'package_size', 'pack', 'packing'
    ],
    'mrp': [
        'mrp', 'price', 'product_price', 'unit_price'
    ],
}

REQUIRED_CANONICAL_FIELDS = [
    'medicine_name',
    'batch_number',
    'expiry_date',
    'quantity',
]

OPTIONAL_CANONICAL_FIELDS = [
    'strength',
    'unit',
    'low_stock_limit',
    'composition',
    'manufacturer',
    'dosage_form',
    'pack_size',
    'mrp',
]


def normalize_column_name(col_name: str) -> str:
    """Normalizes column names for alias matching (lowercase, underscores, no punctuation)."""
    if not col_name:
        return ""
    # Strip whitespace and BOM characters
    s = col_name.strip().lstrip('\ufeff')
    s = s.lower()
    # Replace spaces, hyphens, slashes with underscores
    s = re.sub(r'[\s\-\/\.]+', '_', s)
    # Strip non-alphanumeric except underscore
    s = re.sub(r'[^\w]', '', s)
    return s.strip('_')


def detect_csv_encoding_and_delimiter(file_bytes: bytes) -> Tuple[str, str, str]:
    """
    Detects encoding (utf-8-sig, utf-8, or latin-1) and delimiter (comma, semicolon, tab).
    Returns (encoding, delimiter, decoded_text).
    """
    # 1. Encoding Detection
    if file_bytes.startswith(b'\xef\xbb\xbf'):
        encoding = 'utf-8-sig'
    else:
        try:
            file_bytes.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            encoding = 'latin-1'

    decoded_text = file_bytes.decode(encoding)

    # 2. Delimiter Detection
    sample = decoded_text[:4096]
    # Check first line
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    
    # Try csv.Sniffer with candidate delimiters
    candidates = [',', ';', '\t']
    delimiter = ','
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=',;\t')
        if dialect.delimiter in candidates:
            delimiter = dialect.delimiter
    except Exception:
        # Fallback heuristic: count delimiters in the first line
        counts = {d: first_line.count(d) for d in candidates}
        best = max(counts, key=counts.get)
        if counts[best] > 0:
            delimiter = best

    return encoding, delimiter, decoded_text


def auto_detect_column_mapping(headers: List[str]) -> Dict[str, Optional[str]]:
    """
    Analyzes raw CSV headers and matches them to canonical inventory fields.
    Returns dict: { canonical_field: csv_header_name or None }
    """
    mapping: Dict[str, Optional[str]] = {
        'medicine_name': None,
        'strength': None,
        'batch_number': None,
        'expiry_date': None,
        'quantity': None,
        'unit': None,
        'low_stock_limit': None,
        'composition': None,
        'manufacturer': None,
        'dosage_form': None,
        'pack_size': None,
        'mrp': None,
    }

    normalized_headers = {normalize_column_name(h): h for h in headers}

    for canonical, aliases in COLUMN_ALIASES.items():
        # Exact alias match first
        for alias in aliases:
            if alias in normalized_headers:
                mapping[canonical] = normalized_headers[alias]
                break

        # If not matched, try substring containment
        if not mapping[canonical]:
            for norm_h, original_h in normalized_headers.items():
                if any(alias in norm_h for alias in aliases):
                    mapping[canonical] = original_h
                    break

    return mapping


def check_catalogue_duplicate(
    medicine_name: str,
    strength: Optional[str] = None,
    dosage_form: Optional[str] = None
) -> Dict[str, Any]:
    """
    Checks if a medicine already exists in the master catalogue using:
    medicine_name (iexact) + strength (iexact/empty) + dosage_form (iexact/empty).
    Also searches for similar candidates matching medicine_name.
    """
    name = (medicine_name or '').strip()
    st = (strength or '').strip()
    df = (dosage_form or '').strip()

    if not name:
        return {'exact_match': None, 'similar_candidates': []}

    dup_query = Q(medicine_name__iexact=name)
    if st:
        dup_query &= Q(strength__iexact=st)
    else:
        dup_query &= (Q(strength__isnull=True) | Q(strength=''))

    if df:
        dup_query &= Q(dosage_form__iexact=df)

    exact = Medicine.objects.filter(dup_query).first()
    exact_match = None
    if exact:
        exact_match = {
            'id': exact.id,
            'medicine_name': exact.medicine_name,
            'strength': exact.strength or '',
            'dosage_form': exact.dosage_form or '',
            'composition': exact.composition or '',
            'manufacturer': exact.manufacturer or '',
            'pack_size': exact.pack_size or '',
            'mrp': str(exact.mrp) if exact.mrp is not None else '',
        }

    # Similar candidates (exact name substring or significant brand keywords)
    sim_q = Q(medicine_name__icontains=name)
    words = [w for w in re.split(r'[\s\-_]+', name) if len(w) >= 4 and not re.match(r'^\d+(?:mg|ml|g|tab|cap)?$', w, flags=re.IGNORECASE)]
    for w in words[:2]:
        sim_q |= Q(medicine_name__icontains=w)

    sim_qs = Medicine.objects.filter(sim_q)
    if exact:
        sim_qs = sim_qs.exclude(pk=exact.id)

    similar_candidates = []
    for med in sim_qs[:5]:
        similar_candidates.append({
            'id': med.id,
            'medicine_name': med.medicine_name,
            'strength': med.strength or '',
            'dosage_form': med.dosage_form or '',
            'composition': med.composition or '',
            'manufacturer': med.manufacturer or '',
            'pack_size': med.pack_size or '',
            'mrp': str(med.mrp) if med.mrp is not None else '',
        })

    return {
        'exact_match': exact_match,
        'similar_candidates': similar_candidates,
    }


def parse_flexible_expiry_date(date_str: Any) -> Tuple[Optional[date], Optional[str]]:
    """
    Parses various date formats commonly used in pharmacy invoices/CSVs:
    - DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY
    - YYYY-MM-DD, YYYY/MM/DD
    - MM/YYYY, MM-YYYY (converts to last day of the month)
    - Named month formats: Dec 2026, December 2026, 15 Dec 2026
    Returns (datetime.date, error_message).
    """
    if not date_str:
        return None, "Expiry date is missing."

    s = str(date_str).strip()
    if not s:
        return None, "Expiry date is empty."

    # 1. Format: MM/YYYY or MM-YYYY (Month & Year only -> last day of month)
    m_month_year = re.match(r'^(0?[1-9]|1[0-2])[\/\-](20\d{2}|\d{2})$', s)
    if m_month_year:
        month = int(m_month_year.group(1))
        year = int(m_month_year.group(2))
        if year < 100:
            year += 2000
        last_day = calendar.monthrange(year, month)[1]
        try:
            return date(year, month, last_day), None
        except ValueError as e:
            return None, f"Invalid date: {e}"

    # 2. Format: Named Month & Year, e.g. "Dec 2026", "December 2026"
    m_named = re.match(r'^([a-z]+)\s*[\/\-,\s]\s*(20\d{2}|\d{2})$', s, flags=re.IGNORECASE)
    if m_named:
        month_name = m_named.group(1).lower()
        year = int(m_named.group(2))
        if year < 100:
            year += 2000
        # Parse month name
        for fmt in ('%b', '%B'):
            try:
                dt = datetime.strptime(month_name, fmt)
                month = dt.month
                last_day = calendar.monthrange(year, month)[1]
                return date(year, month, last_day), None
            except ValueError:
                continue

    # 3. Standard full date patterns
    date_patterns = [
        '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y',
        '%Y-%m-%d', '%Y/%m/%d',
        '%d-%m-%y', '%d/%m/%y',
        '%d %b %Y', '%d %B %Y',
        '%b %d, %Y', '%B %d, %Y',
    ]

    for fmt in date_patterns:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date(), None
        except ValueError:
            continue

    return None, f"Unrecognized or invalid expiry date format: '{s}'."


def read_csv_rows(decoded_text: str, delimiter: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Parses CSV text safely, handling quotes and multiline fields.
    Returns (headers, list of raw row dicts).
    """
    stream = io.StringIO(decoded_text.lstrip('\ufeff'))
    reader = csv.reader(stream, delimiter=delimiter)
    
    rows = []
    headers = []
    
    for row_idx, row in enumerate(reader):
        if not row:
            continue
        # Skip completely empty rows
        if not any(cell.strip() for cell in row):
            continue

        if not headers:
            headers = [h.strip() for h in row]
            continue

        # Pad row with empty strings if shorter than headers
        if len(row) < len(headers):
            row = row + [''] * (len(headers) - len(row))

        row_dict = {headers[i]: row[i].strip() for i in range(len(headers))}
        rows.append(row_dict)

    return headers, rows


def validate_and_preview_csv(
    rows: List[Dict[str, str]],
    mapping: Dict[str, Optional[str]],
    pharmacy_profile
) -> Dict[str, Any]:
    """
    Validates CSV rows against the confirmed column mapping and pharmacy inventory constraints.
    Only Medicine Name, Batch Number, Expiry Date, and Quantity are mandatory for stock import.
    All catalogue fields (Composition, Manufacturer, Strength, Dosage Form, Pack Size, MRP) are optional.
    Bypasses catalogue matching and deduplication during CSV import.
    Checks in-file duplicate batches and existing pharmacy duplicate batches.
    Returns preview data, statistics, missing medicine rows (ready for review/import), and invalid rows.
    """
    preview_rows = []
    matched_rows = []
    missing_medicine_rows = []
    invalid_rows = []

    valid_count = 0
    invalid_count = 0
    unmatched_count = 0
    duplicate_count = 0

    seen_in_file_batches = set()

    for idx, row in enumerate(rows, start=1):
        # Extract stock values using mapping
        raw_med_name = (row.get(mapping.get('medicine_name') or '') or '').strip()
        raw_strength = (row.get(mapping.get('strength') or '') or '').strip() if mapping.get('strength') else ''
        raw_batch = (row.get(mapping.get('batch_number') or '') or '').strip()
        raw_expiry = (row.get(mapping.get('expiry_date') or '') or '').strip()
        raw_quantity = (row.get(mapping.get('quantity') or '') or '').strip()
        raw_unit = (row.get(mapping.get('unit') or '') or '').strip() if mapping.get('unit') else 'Units'
        raw_limit = (row.get(mapping.get('low_stock_limit') or '') or '').strip() if mapping.get('low_stock_limit') else '10'

        # Extract optional catalogue values
        raw_composition = (row.get(mapping.get('composition') or '') or '').strip() if mapping.get('composition') else ''
        raw_manufacturer = (row.get(mapping.get('manufacturer') or '') or '').strip() if mapping.get('manufacturer') else ''
        raw_dosage_form = (row.get(mapping.get('dosage_form') or '') or '').strip() if mapping.get('dosage_form') else ''
        raw_pack_size = (row.get(mapping.get('pack_size') or '') or '').strip() if mapping.get('pack_size') else ''
        raw_mrp = (row.get(mapping.get('mrp') or '') or '').strip() if mapping.get('mrp') else ''

        row_errors = []
        row_warnings = []
        stock_errors = []
        status = 'VALID'

        # 1. Validate Medicine Name presence (mandatory)
        if not raw_med_name:
            row_errors.append("Medicine name is missing.")
            status = 'INVALID'

        # 2. Validate Batch Number (mandatory)
        if not raw_batch:
            stock_errors.append("Batch number is required.")

        # 3. Validate Expiry Date (mandatory & parseable)
        parsed_exp, exp_err = parse_flexible_expiry_date(raw_expiry)
        if exp_err:
            stock_errors.append(exp_err)

        # 4. Validate Quantity (mandatory & non-negative integer)
        parsed_qty = None
        if not raw_quantity:
            stock_errors.append("Quantity is required.")
        else:
            try:
                parsed_qty = int(Decimal(raw_quantity))
                if parsed_qty < 0:
                    stock_errors.append("Quantity cannot be negative.")
            except Exception:
                stock_errors.append(f"Invalid quantity: '{raw_quantity}'. Must be a whole number.")

        # 5. Validate Unit
        unit_val = raw_unit if raw_unit else 'Units'
        if len(unit_val) > 50:
            unit_val = unit_val[:50]

        # 6. Validate Low Stock Limit
        parsed_limit = 10
        if raw_limit:
            try:
                parsed_limit = int(Decimal(raw_limit))
                if parsed_limit < 0:
                    parsed_limit = 10
            except Exception:
                parsed_limit = 10

        # 7. Validate Optional MRP (if provided, must be > 0)
        catalogue_errors = []
        if raw_mrp:
            try:
                if Decimal(raw_mrp) <= Decimal('0.00'):
                    catalogue_errors.append("MRP must be greater than zero.")
            except Exception:
                catalogue_errors.append(f"Invalid MRP format: '{raw_mrp}'.")

        if catalogue_errors:
            row_errors.extend(catalogue_errors)
            status = 'INVALID'

        # Merge stock errors into row errors if stock is invalid
        if stock_errors:
            row_errors.extend(stock_errors)
            status = 'INVALID'

        # 8. Check In-File Duplicate Batch (grouped by medicine name + batch within this CSV)
        if status != 'INVALID' and raw_med_name and raw_batch:
            in_file_key = (raw_med_name.strip().lower(), raw_batch.strip().upper())
            if in_file_key in seen_in_file_batches:
                row_warnings.append(f"Duplicate batch '{raw_batch}' appears multiple times in this CSV.")
                status = 'DUPLICATE'
                duplicate_count += 1
            else:
                seen_in_file_batches.add(in_file_key)

        # 9. Check Existing Database Duplicate Batch for this pharmacy
        if status not in ('INVALID', 'DUPLICATE') and raw_med_name and raw_batch:
            exists_in_db = Inventory.objects.filter(
                pharmacy=pharmacy_profile,
                medicine__medicine_name__iexact=raw_med_name,
                batch_number=raw_batch
            ).exists()
            if exists_in_db:
                row_warnings.append(f"Batch '{raw_batch}' already exists in your pharmacy inventory. Will not overwrite.")
                status = 'DUPLICATE'
                duplicate_count += 1

        stock_is_valid = len(stock_errors) == 0 and bool(raw_med_name)

        if status == 'VALID':
            valid_count += 1
            unmatched_count += 1
        elif status == 'INVALID':
            invalid_count += 1

        catalogue_data = {
            'medicine_name': raw_med_name,
            'composition': raw_composition,
            'manufacturer': raw_manufacturer,
            'strength': raw_strength,
            'dosage_form': raw_dosage_form,
            'pack_size': raw_pack_size,
            'mrp': raw_mrp,
        }

        missing_required_fields = []
        if not raw_med_name:
            missing_required_fields.append('medicine_name')

        row_item = {
            'row_num': idx,
            'original_medicine': raw_med_name,
            'original_strength': raw_strength,
            'matched_medicine': None,
            'candidates': [],
            'batch_number': raw_batch,
            'expiry_date_raw': raw_expiry,
            'expiry_date_formatted': parsed_exp.isoformat() if parsed_exp else None,
            'quantity': parsed_qty if parsed_qty is not None else raw_quantity,
            'unit': unit_val,
            'low_stock_limit': parsed_limit,
            'status': status,
            'errors': row_errors,
            'warnings': row_warnings,
            'catalogue_data': catalogue_data,
            'missing_required_fields': missing_required_fields,
            'catalogue_errors': catalogue_errors,
            'duplicate_warning': None,
            'exact_match': None,
            'similar_candidates': [],
            'stock_is_valid': stock_is_valid,
            'stock_errors': stock_errors,
        }

        preview_rows.append(row_item)

        if status == 'INVALID' or status == 'DUPLICATE':
            invalid_rows.append(row_item)
        else:
            missing_medicine_rows.append(row_item)
            matched_rows.append(row_item)

    return {
        'total_rows': len(rows),
        'valid_count': valid_count,
        'invalid_count': invalid_count,
        'unmatched_count': unmatched_count,
        'duplicate_count': duplicate_count,
        'ready_to_import_count': valid_count,
        'preview_rows': preview_rows,
        'matched_rows': matched_rows,
        'missing_medicine_rows': missing_medicine_rows,
        'invalid_rows': invalid_rows,
    }


def execute_csv_stock_import(
    validated_rows: List[Dict[str, Any]],
    pharmacy_profile,
    new_medicines: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Executes the final import of confirmed stock rows and new medicines into
    the master catalogue and the authenticated pharmacy's Inventory.
    Revalidates every row immediately prior to saving.
    Only Medicine Name is mandatory for catalogue creation; all other fields
    (composition, manufacturer, strength, dosage_form, pack_size, mrp) are optional.
    Groups multiple batches for the same medicine name within one CSV run to create
    exactly ONE Medicine record and multiple Inventory batch records.
    Never overwrites existing pharmacy stock.
    Runs inside safe savepoints so row-level failures do not block other valid rows.
    """
    imported_rows = []
    skipped_rows = []
    failed_rows = []
    new_medicines_created_count = 0
    existing_medicines_imported_count = 0

    # In-memory dictionary to group multiple batches of the same medicine within this CSV run
    # Maps clean_medicine_name (lowercase) -> Medicine instance
    created_meds_in_csv: Dict[str, Medicine] = {}
    seen_batches_in_import = set()

    # 1. Process Validated / Matched Rows (require existing medicine_id)
    if validated_rows:
        for item in validated_rows:
            row_num = item.get('row_num', '?')
            med_id = item.get('medicine_id')
            orig_med = item.get('original_medicine', '') or item.get('medicine_name', '')
            batch_num = (item.get('batch_number') or '').strip()
            exp_str = item.get('expiry_date')
            qty = item.get('quantity')
            unit = (item.get('unit') or 'Units').strip()
            limit = item.get('low_stock_limit', 10)

            if not med_id:
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': orig_med,
                    'batch_number': batch_num,
                    'reason': 'No catalogue formulation selected or matched.'
                })
                continue

            med = Medicine.objects.filter(pk=med_id).first()
            if not med:
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': orig_med,
                    'batch_number': batch_num,
                    'reason': f"Medicine ID {med_id} does not exist in master catalogue."
                })
                continue

            if not batch_num:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': '',
                    'reason': 'Batch number is empty.'
                })
                continue

            parsed_exp, exp_err = parse_flexible_expiry_date(exp_str)
            if exp_err or not parsed_exp:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': exp_err or 'Invalid expiry date.'
                })
                continue

            try:
                qty_int = int(qty)
                if qty_int < 0:
                    raise ValueError("Quantity cannot be negative.")
            except Exception:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Invalid quantity: '{qty}'."
                })
                continue

            batch_key = (med.id, batch_num.upper())
            if batch_key in seen_batches_in_import:
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Duplicate batch '{batch_num}' appears multiple times in this CSV. Skipped to prevent duplicate stock."
                })
                continue
            seen_batches_in_import.add(batch_key)

            if Inventory.objects.filter(pharmacy=pharmacy_profile, medicine=med, batch_number=batch_num).exists():
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Batch '{batch_num}' already exists in your inventory. Skipped to prevent duplicate stock."
                })
                continue

            try:
                with transaction.atomic():
                    inv = Inventory.objects.create(
                        pharmacy=pharmacy_profile,
                        medicine=med,
                        batch_number=batch_num,
                        expiry_date=parsed_exp,
                        quantity=qty_int,
                        unit=unit[:50] if unit else 'Units',
                        low_stock_limit=int(limit) if limit else 10
                    )
                    imported_rows.append({
                        'row_num': row_num,
                        'inventory_id': inv.id,
                        'medicine_name': med.medicine_name,
                        'batch_number': inv.batch_number,
                        'quantity': inv.quantity,
                        'unit': inv.unit,
                        'expiry_date': inv.expiry_date.isoformat(),
                    })
                    existing_medicines_imported_count += 1
            except Exception as exc:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Database write error: {str(exc)}"
                })

    # 2. Process New Medicines (Missing / New Catalogue Medicines & Batches)
    if new_medicines:
        for item in new_medicines:
            row_num = item.get('row_num', '?')
            stock_data = item.get('stock') or item
            batch_num = (stock_data.get('batch_number') or item.get('batch_number') or '').strip()
            exp_str = stock_data.get('expiry_date') or item.get('expiry_date')
            qty = stock_data.get('quantity') if stock_data.get('quantity') is not None else item.get('quantity')
            unit = (stock_data.get('unit') or item.get('unit') or 'Units').strip()
            limit = stock_data.get('low_stock_limit') if stock_data.get('low_stock_limit') is not None else item.get('low_stock_limit', 10)

            cat_data = item.get('catalogue') or item
            med_name = (cat_data.get('medicine_name') or item.get('medicine_name') or item.get('original_medicine') or '').strip()
            composition = (cat_data.get('composition') or item.get('composition') or '').strip()
            manufacturer = (cat_data.get('manufacturer') or item.get('manufacturer') or '').strip()
            strength = (cat_data.get('strength') or item.get('strength') or '').strip()
            dosage_form = (cat_data.get('dosage_form') or item.get('dosage_form') or '').strip()
            pack_size = (cat_data.get('pack_size') or item.get('pack_size') or '').strip()
            mrp_val = cat_data.get('mrp') if cat_data.get('mrp') is not None else item.get('mrp')
            use_existing_id = cat_data.get('use_existing_medicine_id') or item.get('use_existing_medicine_id') or item.get('medicine_id')

            # --- A. STOCK VALIDATION FIRST ---
            # If stock data is invalid, NEVER create Medicine catalogue record!
            if not batch_num:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med_name or 'Unknown',
                    'batch_number': '',
                    'reason': 'Batch number is empty. Catalogue medicine was NOT created.'
                })
                continue

            parsed_exp, exp_err = parse_flexible_expiry_date(exp_str)
            if exp_err or not parsed_exp:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med_name or 'Unknown',
                    'batch_number': batch_num,
                    'reason': f"{exp_err or 'Invalid expiry date'}. Catalogue medicine was NOT created."
                })
                continue

            try:
                qty_int = int(qty)
                if qty_int < 0:
                    raise ValueError("Quantity cannot be negative.")
            except Exception:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med_name or 'Unknown',
                    'batch_number': batch_num,
                    'reason': f"Invalid quantity: '{qty}'. Catalogue medicine was NOT created."
                })
                continue

            if not med_name and not use_existing_id:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': 'Unknown',
                    'batch_number': batch_num,
                    'reason': 'Medicine name is required for stock import.'
                })
                continue

            # MRP validation (optional; if provided, must be positive)
            parsed_mrp = None
            if mrp_val is not None and str(mrp_val).strip() != '':
                try:
                    parsed_mrp = Decimal(str(mrp_val).strip())
                    if parsed_mrp <= Decimal('0.00'):
                        failed_rows.append({
                            'row_num': row_num,
                            'medicine_name': med_name or 'Unknown',
                            'batch_number': batch_num,
                            'reason': 'MRP must be greater than zero.'
                        })
                        continue
                except Exception:
                    failed_rows.append({
                        'row_num': row_num,
                        'medicine_name': med_name or 'Unknown',
                        'batch_number': batch_num,
                        'reason': f"Invalid MRP value: '{mrp_val}'."
                    })
                    continue

            # --- B. CATALOGUE RECORD RESOLUTION ---
            med = None
            clean_key = med_name.lower().strip() if med_name else ''

            if use_existing_id:
                med = Medicine.objects.filter(pk=use_existing_id).first()
                if not med:
                    failed_rows.append({
                        'row_num': row_num,
                        'medicine_name': med_name or f"ID {use_existing_id}",
                        'batch_number': batch_num,
                        'reason': f"Selected catalogue candidate ID {use_existing_id} was not found."
                    })
                    continue
                existing_medicines_imported_count += 1
            elif clean_key in created_meds_in_csv:
                # Multiple batches for the same medicine within this CSV run -> reuse the single created Medicine record
                med = created_meds_in_csv[clean_key]
            else:
                # Create new Medicine record in shared master catalogue
                # Only Medicine Name is mandatory. Composition, Manufacturer, Strength, etc. are optional.
                try:
                    with transaction.atomic():
                        med = Medicine.objects.create(
                            medicine_name=med_name,
                            composition=composition if composition else None,
                            manufacturer=manufacturer if manufacturer else None,
                            strength=strength if strength else None,
                            dosage_form=dosage_form if dosage_form else None,
                            pack_size=pack_size if pack_size else None,
                            mrp=parsed_mrp,
                        )
                        created_meds_in_csv[clean_key] = med
                        new_medicines_created_count += 1
                except Exception as exc:
                    failed_rows.append({
                        'row_num': row_num,
                        'medicine_name': med_name,
                        'batch_number': batch_num,
                        'reason': f"Error creating catalogue medicine: {str(exc)}"
                    })
                    continue

            # --- C. INVENTORY CREATION & DEDUPLICATION ---
            # 1. In-file duplicate batch check
            batch_key = (med.id, batch_num.upper())
            if batch_key in seen_batches_in_import:
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Duplicate batch '{batch_num}' appears multiple times in this CSV. Skipped to prevent duplicate stock."
                })
                continue
            seen_batches_in_import.add(batch_key)

            # 2. Existing database batch check for this pharmacy
            if Inventory.objects.filter(pharmacy=pharmacy_profile, medicine=med, batch_number=batch_num).exists():
                skipped_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Batch '{batch_num}' already exists in your inventory. Skipped to prevent duplicate stock."
                })
                continue

            try:
                with transaction.atomic():
                    inv = Inventory.objects.create(
                        pharmacy=pharmacy_profile,
                        medicine=med,
                        batch_number=batch_num,
                        expiry_date=parsed_exp,
                        quantity=qty_int,
                        unit=unit[:50] if unit else 'Units',
                        low_stock_limit=int(limit) if limit else 10
                    )
                    imported_rows.append({
                        'row_num': row_num,
                        'inventory_id': inv.id,
                        'medicine_name': med.medicine_name,
                        'batch_number': inv.batch_number,
                        'quantity': inv.quantity,
                        'unit': inv.unit,
                        'expiry_date': inv.expiry_date.isoformat(),
                    })
            except Exception as exc:
                failed_rows.append({
                    'row_num': row_num,
                    'medicine_name': med.medicine_name,
                    'batch_number': batch_num,
                    'reason': f"Database write error for inventory: {str(exc)}"
                })

    return {
        'total_submitted': (len(validated_rows) if validated_rows else 0) + (len(new_medicines) if new_medicines else 0),
        'imported_count': len(imported_rows),
        'skipped_count': len(skipped_rows),
        'failed_count': len(failed_rows),
        'new_medicines_created_count': new_medicines_created_count,
        'existing_medicines_imported_count': existing_medicines_imported_count,
        'stock_batches_imported_count': len(imported_rows),
        'imported_rows': imported_rows,
        'skipped_rows': skipped_rows,
        'failed_rows': failed_rows,
    }

    return {
        'total_submitted': len(all_items),
        'imported_count': len(imported_rows),
        'skipped_count': len(skipped_rows),
        'failed_count': len(failed_rows),
        'new_medicines_created_count': new_medicines_created_count,
        'existing_medicines_imported_count': existing_medicines_imported_count,
        'stock_batches_imported_count': len(imported_rows),
        'imported_rows': imported_rows,
        'skipped_rows': skipped_rows,
        'failed_rows': failed_rows,
    }


def generate_skipped_rows_csv(skipped_or_failed_items: List[Dict[str, Any]]) -> str:
    """
    Generates CSV text summarizing skipped/failed rows with reason descriptions.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Row_Number', 'Medicine_Name', 'Batch_Number', 'Status_Reason'])

    for item in skipped_or_failed_items:
        writer.writerow([
            item.get('row_num', ''),
            item.get('medicine_name', ''),
            item.get('batch_number', ''),
            item.get('reason', '')
        ])

    return output.getvalue()

