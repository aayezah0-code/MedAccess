"""
Voice Stock Parser & Information Extraction Service for MedAccess Pharmacy.

Provides rule-based extraction of medicine inventory parameters from spoken
English, Hindi, and Hinglish transcripts:
- medicine_name
- strength
- quantity
- packaging unit
- batch_number
- expiry_date

Zero external AI/NLP APIs. Uses pure Python stdlib and regular expressions.
"""

import re
import calendar
from datetime import datetime
from typing import Optional, Dict, Any, Tuple


# Standard packaging units supported by pharmacy inventory
UNIT_MAP = {
    'strip': 'Strips',
    'strips': 'Strips',
    'patta': 'Strips',
    'patte': 'Strips',
    'tablet': 'Tablets',
    'tablets': 'Tablets',
    'tab': 'Tablets',
    'tabs': 'Tablets',
    'goli': 'Tablets',
    'goliya': 'Tablets',
    'goliyan': 'Tablets',
    'capsule': 'Capsules',
    'capsules': 'Capsules',
    'cap': 'Capsules',
    'caps': 'Capsules',
    'box': 'Boxes',
    'boxes': 'Boxes',
    'dibba': 'Boxes',
    'dibbe': 'Boxes',
    'bottle': 'Bottles',
    'bottles': 'Bottles',
    'shishi': 'Bottles',
    'vial': 'Vials',
    'vials': 'Vials',
    'ampoule': 'Ampoules',
    'ampoules': 'Ampoules',
    'piece': 'Pieces',
    'pieces': 'Pieces',
    'pc': 'Pieces',
    'pcs': 'Pieces',
    'pack': 'Packs',
    'packs': 'Packs',
    'packet': 'Packs',
    'packets': 'Packs',
    'unit': 'Units',
    'units': 'Units',
}

MONTH_MAP = {
    'january': 1, 'jan': 1,
    'february': 2, 'feb': 2,
    'march': 3, 'mar': 3,
    'april': 4, 'apr': 4,
    'may': 5,
    'june': 6, 'jun': 6,
    'july': 7, 'jul': 7,
    'august': 8, 'aug': 8,
    'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10,
    'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}

WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80,
    'ninety': 90, 'hundred': 100, 'two hundred': 200, 'five hundred': 500,
}


def extract_batch_number(text: str) -> Tuple[Optional[str], str]:
    """
    Extracts batch / lot number from transcript and removes it from the text.
    Handles English and Hinglish expressions like:
    - 'batch number PARA-101'
    - 'is medicine ka batch number PARA-101 hai'
    - 'batch no CRO-101'
    - 'lot number BATCH-99'
    """
    # Pattern matching "batch number", "batch no", "batch", "lot number" followed by identifier
    batch_pattern = re.compile(
        r'(?i)\b(?:is\s+(?:medicine|dawa|item|product)\s+ka\s+)?(?:batch\s*(?:number|no|num)?|lot\s*(?:number|no|num)?)\s*(?:is|=|:|-)?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{1,30})\b(?:\s+(?:hai|hain|tha|hoga))?'
    )

    match = batch_pattern.search(text)
    if match:
        raw_val = match.group(1).strip()
        # Clean trailing punctuation
        clean_batch = re.sub(r'[\.,;:]$', '', raw_val).upper()
        # Filter out accidental stop words that might match
        if clean_batch.lower() not in ('hai', 'hain', 'tha', 'aur', 'and', 'with', 'expiry', 'quantity'):
            text_remaining = text[:match.start()] + ' ' + text[match.end():]
            return clean_batch, text_remaining

    return None, text


def extract_expiry_date(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Extracts expiry date from transcript and returns (dict, remaining_text).
    Formats supported:
    - Named month & year: 'December 2026', 'Dec 2026', 'December 26'
    - Slash/dash MM/YYYY or MM/YY: '12/2026', '12/26', '12-2026'
    - Optional full date: '15 December 2026', '15/12/2026'
    - Preceded or followed by 'expiry', 'exp', 'expiry date', 'hai', 'hain'
    """
    months_regex = '|'.join(MONTH_MAP.keys())

    # 1. Pattern: Month Name + Year (e.g., "December 2026", "Dec 2026", "December 26")
    named_month_pattern = re.compile(
        rf'(?i)\b(?:expiry\s*(?:date)?|exp\s*(?:date)?)\s*(?:is|=|:|-)?\s*(?:([0-3]?[0-9])(?:st|nd|rd|th)?[\s\/\-]+)?({months_regex})\s*,?\s*(\d{{2,4}})(?:\s+(?:hai|hain|tha))?\b'
    )
    match = named_month_pattern.search(text)

    # Fallback pattern without explicit 'expiry' trigger if month and year are together
    if not match:
        standalone_named_month = re.compile(
            rf'(?i)\b(?:expiry\s*(?:date)?|exp\s*(?:date)?)\s*(?:is|=|:|-)?\s*({months_regex})\s+(\d{{2,4}})(?:\s+(?:hai|hain|tha))?\b'
        )
        match = standalone_named_month.search(text)
        if match:
            day_str = None
            month_str = match.group(1).lower()
            year_str = match.group(2)
        else:
            day_str = None
            month_str = None
            year_str = None
    else:
        day_str = match.group(1)
        month_str = match.group(2).lower()
        year_str = match.group(3)

    if month_str and year_str:
        month = MONTH_MAP.get(month_str, 1)
        year = int(year_str)
        if year < 100:
            year += 2000

        if day_str and day_str.isdigit():
            day = int(day_str)
            max_day = calendar.monthrange(year, month)[1]
            day = min(day, max_day)
        else:
            # When only month/year is stated, standard medical expiry is the last day of the month
            day = calendar.monthrange(year, month)[1]

        formatted_date = f"{year:04d}-{month:02d}-{day:02d}"
        raw_text = match.group(0).strip()
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return {
            'raw': f"{month_str.capitalize()} {year}",
            'formatted': formatted_date
        }, text_remaining

    # 2. Pattern: Numeric MM/YYYY or MM/YY with expiry trigger (e.g., "expiry 12/2026", "exp 12/26")
    numeric_pattern = re.compile(
        r'(?i)\b(?:expiry\s*(?:date)?|exp\s*(?:date)?)\s*(?:is|=|:|-)?\s*(0?[1-9]|1[0-2])[\/\-](20\d{2}|\d{2})\b(?:\s+(?:hai|hain|tha))?'
    )
    match = numeric_pattern.search(text)
    if match:
        month = int(match.group(1))
        year = int(match.group(2))
        if year < 100:
            year += 2000
        day = calendar.monthrange(year, month)[1]
        formatted_date = f"{year:04d}-{month:02d}-{day:02d}"
        month_name = calendar.month_name[month]
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return {
            'raw': f"{month_name} {year}",
            'formatted': formatted_date
        }, text_remaining

    return None, text


def extract_strength(text: str) -> Tuple[Optional[str], str]:
    """
    Extracts pharmaceutical strength from transcript and returns (strength, remaining_text).
    Examples:
    - '500 mg', '500mg', '650 mg', '10 ml', '250mg/5ml', '0.5%'
    Ensures strength digits are NOT confused with stock quantities.
    """
    strength_pattern = re.compile(
        r'(?i)\b(\d+(?:\.\d+)?)\s*(mg|mcg|gm|g|ml|iu|%)(?:\s*\/\s*(\d+(?:\.\d+)?)\s*(mg|mcg|gm|g|ml))?\b'
    )
    match = strength_pattern.search(text)
    if match:
        strength_val = match.group(0).strip()
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return strength_val, text_remaining

    return None, text


def extract_quantity_and_unit(text: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    Extracts stock quantity and packaging unit from transcript and returns
    (quantity, unit, remaining_text).
    Handles:
    - '50 strips add karo'
    - 'ke 20 boxes hain'
    - 'quantity 100 tablets'
    - 'quantity is 100'
    - 'fifty strips'
    """
    unit_keys_regex = '|'.join(sorted(UNIT_MAP.keys(), key=lambda x: -len(x)))

    # 1. Pattern: "quantity is 100 tablets" or "quantity 100"
    qty_keyword_pattern = re.compile(
        rf'(?i)\b(?:quantity|qty|matra)\s*(?:is|=|:)?\s*(\d+)\s*({unit_keys_regex})?\b'
    )
    match = qty_keyword_pattern.search(text)
    if match:
        qty = int(match.group(1))
        unit_raw = match.group(2)
        unit = UNIT_MAP.get(unit_raw.lower(), 'Units') if unit_raw else None
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return qty, unit, text_remaining

    # 2. Pattern: Number followed immediately by unit (e.g. "50 strips", "20 boxes", "100 tablets")
    num_unit_pattern = re.compile(
        rf'(?i)\b(\d+)\s+({unit_keys_regex})\b'
    )
    match = num_unit_pattern.search(text)
    if match:
        qty = int(match.group(1))
        unit_raw = match.group(2).lower()
        unit = UNIT_MAP.get(unit_raw, 'Units')
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return qty, unit, text_remaining

    # 3. Word numbers (e.g., "fifty strips")
    for word_num, val in WORD_NUMBERS.items():
        word_pattern = re.compile(
            rf'(?i)\b{re.escape(word_num)}\s+({unit_keys_regex})\b'
        )
        match = word_pattern.search(text)
        if match:
            unit_raw = match.group(1).lower()
            unit = UNIT_MAP.get(unit_raw, 'Units')
            text_remaining = text[:match.start()] + ' ' + text[match.end():]
            return val, unit, text_remaining

    # 4. Standalone unit without quantity (e.g. pharmacist said unit separately)
    standalone_unit_pattern = re.compile(
        rf'(?i)\b({unit_keys_regex})\b'
    )
    match = standalone_unit_pattern.search(text)
    if match:
        unit_raw = match.group(1).lower()
        unit = UNIT_MAP.get(unit_raw, 'Units')
        text_remaining = text[:match.start()] + ' ' + text[match.end():]
        return None, unit, text_remaining

    return None, None, text


def extract_medicine_name(text: str) -> Optional[str]:
    """
    Cleans residual text to isolate the medicine brand or trade name.
    Strips English and Hinglish conversational words:
    - 'add karo', 'daalo', 'ki', 'ke', 'ka', 'hai', 'hain'
    - 'is medicine ka', 'medicine', 'dawa'
    - 'and', 'aur', 'with', 'of', 'for'
    """
    # List of conversational fillers, action verbs, and prepositions to remove
    fillers = [
        r'\bis\s+(?:medicine|dawa|dawaii|product|item)\s*(?:ka|ki|ke)?\b',
        r'\b(?:add\s*karo|add\s*kardo|add\s*kar\s*do|add\s*karna\s*hai|add)\b',
        r'\b(?:daalo|daal\s*do|daalna\s*hai|daal|jodo|chahiye|rakho|bhejo)\b',
        r'\b(?:ki|ke|ka|ko|mein|se|hai|hain|tha|the|hoga)\b',
        r'\b(?:aur|tatha|and|with|of|for|please|kripya)\b',
        r'\b(?:medicine|dawa|dawaii|product|item)\b',
        r'\b(?:quantity|qty|matra)\b',
        r'\b(?:batch\s*(?:number|no|num)?|lot\s*(?:number|no|num)?)\b',
        r'\b(?:expiry\s*(?:date)?|exp\s*(?:date)?)\b',
    ]

    cleaned = text
    for pattern in fillers:
        cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE)

    # Remove extra punctuation and multiple whitespaces
    cleaned = re.sub(r'[^\w\s\+\-\/\.]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # If residual has substantive alphabetical characters and length >= 2
    if len(cleaned) >= 2 and re.search(r'[a-zA-Z]', cleaned):
        # Clean leading/trailing punctuation
        cleaned = re.sub(r'^[\W_]+|[\W_]+$', '', cleaned)
        if len(cleaned) >= 2:
            # Capitalize each word nicely (e.g. 'paracetamol' -> 'Paracetamol')
            return ' '.join(word.capitalize() for word in cleaned.split())

    return None


def parse_voice_command(transcript: str) -> Dict[str, Any]:
    """
    Main parser entrypoint.
    Executes a structured, safe extraction pipeline over spoken transcripts:
    1. Extract Batch Number (masks digits from quantity/expiry)
    2. Extract Expiry Date (masks year/month from quantity)
    3. Extract Strength (e.g. 500mg, masks from quantity)
    4. Extract Quantity and Packaging Unit
    5. Extract Medicine Name from remaining substantive tokens
    6. Identifies missing fields and returns full structured payload.
    """
    if not transcript or not isinstance(transcript, str):
        return {
            'transcript': '',
            'medicine_name': None,
            'strength': None,
            'quantity': None,
            'unit': None,
            'batch_number': None,
            'expiry_date': None,
            'is_complete': False,
            'missing_fields': ['transcript'],
        }

    working_text = transcript.strip()

    # Step 1: Batch Number
    batch_number, working_text = extract_batch_number(working_text)

    # Step 2: Expiry Date
    expiry_date, working_text = extract_expiry_date(working_text)

    # Step 3: Strength
    strength, working_text = extract_strength(working_text)

    # Step 4: Quantity and Unit
    quantity, unit, working_text = extract_quantity_and_unit(working_text)

    # Step 5: Medicine Name
    medicine_name = extract_medicine_name(working_text)

    # Compile missing fields list for pharmacist guidance
    missing_fields = []
    if not medicine_name:
        missing_fields.append('medicine_name')
    if quantity is None:
        missing_fields.append('quantity')
    if not unit:
        missing_fields.append('unit')
    if not batch_number:
        missing_fields.append('batch_number')
    if not expiry_date:
        missing_fields.append('expiry_date')

    is_complete = len(missing_fields) == 0

    return {
        'transcript': transcript,
        'medicine_name': medicine_name,
        'strength': strength,
        'quantity': quantity,
        'unit': unit,
        'batch_number': batch_number,
        'expiry_date': expiry_date,
        'is_complete': is_complete,
        'missing_fields': missing_fields,
    }

