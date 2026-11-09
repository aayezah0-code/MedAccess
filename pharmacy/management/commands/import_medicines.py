import csv
import os
import re
import time
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pharmacy.models import Medicine

# Pharmaceutical Dosage Forms recognized in Indian and international pharmacopeias
# Ordered with multi-word terms first to avoid partial-matching collisions (e.g. 'Eye Drops' before 'Drops')
DOSAGE_FORMS = [
    'Eye Drops', 'Ear Drops', 'Nasal Drops', 'Nasal Spray', 'Oral Drops',
    'Tablet', 'Capsule', 'Injection', 'Syrup', 'Suspension', 'Ointment',
    'Cream', 'Gel', 'Drops', 'Inhaler', 'Rotacap', 'Respule',
    'Lotion', 'Solution', 'Powder', 'Granules', 'Sachet', 'Flexpen',
    'Penfill', 'Cartridge', 'Emulsion', 'Mouthwash', 'Gargle', 'Patch',
    'Suppository', 'Soap', 'Shampoo', 'Lozenges', 'Liniment', 'Spray'
]

# Reliable regex patterns for extracting pack sizes from product descriptions
PACK_SIZE_PATTERNS = [
    re.compile(r'\b(Strip of \d+(?:\s+\w+)?)\b', re.IGNORECASE),
    re.compile(r'\b(Bottle of \d+(?:\s+\w+)?)\b', re.IGNORECASE),
    re.compile(r'\b(Pack of \d+(?:\s+\w+)?)\b', re.IGNORECASE),
    re.compile(r'\b(Box of \d+(?:\s+\w+)?)\b', re.IGNORECASE),
    re.compile(r'\b(Blister of \d+(?:\s+\w+)?)\b', re.IGNORECASE),
    re.compile(r'\b(\d+\s*ml\s+(?:Bottle|Syrup|Suspension|Drop|Drops|Liquid|Solution))\b', re.IGNORECASE),
    re.compile(r'\b(\d+\s*g\s+(?:Tube|Pack|Ointment|Cream|Gel|Powder|Sachet))\b', re.IGNORECASE),
    re.compile(r'\b(\d+\s*gm\s+(?:Tube|Pack|Ointment|Cream|Gel|Powder|Sachet))\b', re.IGNORECASE),
]

UNIT_REGEX = re.compile(
    r'\(([\d.]+\s*(?:mg|gm|g|mcg|iu/ml|iu|%|ml|w/v|w/w))\)',
    re.IGNORECASE
)

STRENGTH_NAME_REGEX = re.compile(
    r'\b([\d.]+\s*(?:mg|gm|g|mcg|iu/ml|iu))\b',
    re.IGNORECASE
)


def normalize_str(val):
    """Normalize string by lowercasing and collapsing whitespace."""
    if not val:
        return ''
    return ' '.join(str(val).lower().split())


def get_composite_key(name, composition, manufacturer):
    """Generate a composite key tuple for deduplication."""
    return (
        normalize_str(name),
        normalize_str(composition),
        normalize_str(manufacturer),
    )


def extract_dosage_form(product_name):
    """Safely detect dosage form from product name without fabricating information."""
    if not product_name:
        return None
    for form in DOSAGE_FORMS:
        if re.search(r'\b' + re.escape(form) + r'\b', product_name, re.IGNORECASE):
            return form
    return None


def extract_strength(salt_composition, product_name):
    """
    Safely extract pharmaceutical strength.
    First checks parenthesized units in salt_composition (e.g. Paracetamol (500mg)).
    Falls back to unit patterns in product_name.
    """
    if salt_composition:
        matches = UNIT_REGEX.findall(salt_composition)
        if matches:
            res = " + ".join(m.strip() for m in matches)
            if len(res) <= 100:
                return res
            return res[:100].strip()

    if product_name:
        m = STRENGTH_NAME_REGEX.search(product_name)
        if m:
            res = m.group(1).strip()
            if len(res) <= 100:
                return res
            return res[:100].strip()

    return None


def extract_pack_size(product_name):
    """
    Extract pack size only if a clear, high-confidence pattern exists.
    Never fabricates values.
    """
    if not product_name:
        return None
    for pattern in PACK_SIZE_PATTERNS:
        match = pattern.search(product_name)
        if match:
            res = match.group(1).strip()
            if len(res) <= 100:
                return res
            return res[:100].strip()
    return None


def parse_price(raw_price):
    """
    Parse price string into a valid Decimal(10, 2).
    Strips currency symbols (e.g. ₹), commas, and spaces.
    Returns None if missing, blank, or invalid.
    """
    if not raw_price:
        return None
    cleaned = re.sub(r'[^\d.]', '', str(raw_price).strip())
    if not cleaned:
        return None
    try:
        val = Decimal(cleaned).quantize(Decimal('0.01'))
        if Decimal('0.00') < val < Decimal('99999999.99'):
            return val
        return None
    except (InvalidOperation, TypeError):
        return None


class Command(BaseCommand):
    help = (
        'Import medicines from CSV dataset into the Master Medicine Catalogue. '
        'Streams CSV rows, handles deduplication, resolves missing prices, '
        'checks existing database records for idempotency, and supports dry-run mode.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            default=os.path.join(settings.BASE_DIR, 'data', 'medicine_data.csv'),
            help='Path to the medicine CSV dataset (defaults to data/medicine_data.csv).'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Number of records to insert per database batch transaction (default: 1000).'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run the complete parse and validation pipeline without writing anything to the database.'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Optional limit on the number of CSV rows to process (for quick testing).'
        )

    def handle(self, *args, **options):
        csv_file_path = options['file']
        batch_size = options['batch_size']
        dry_run = options['dry_run']
        row_limit = options['limit']

        self.stdout.write(self.style.MIGRATE_HEADING("=== MedAccess Master Medicine Catalogue Importer ==="))
        self.stdout.write(f"Source CSV:   {csv_file_path}")
        self.stdout.write(f"Batch Size:   {batch_size}")
        self.stdout.write(f"Mode:         {'DRY-RUN (Simulated)' if dry_run else 'EXECUTION (Live Database Write)'}")
        if row_limit:
            self.stdout.write(f"Row Limit:    {row_limit}")

        if not os.path.exists(csv_file_path):
            raise CommandError(f"Medicine dataset not found at: {csv_file_path}")

        start_time = time.time()

        # Step 1: Query existing database medicines for idempotency
        self.stdout.write("\nChecking existing database records for idempotency...")
        existing_db_records = Medicine.objects.all().values_list('medicine_name', 'composition', 'manufacturer')
        existing_db_keys = {
            get_composite_key(name, comp, mfr)
            for name, comp, mfr in existing_db_records
        }
        self.stdout.write(f"Found {len(existing_db_keys)} existing medicine record(s) currently in database.")

        # Step 2: Stream and parse the CSV file
        self.stdout.write("\nStreaming and parsing CSV dataset...")

        total_rows_read = 0
        duplicate_rows_skipped = 0
        malformed_rows_rejected = 0
        candidates = {}  # composite_key -> candidate data dict

        try:
            with open(csv_file_path, mode='r', encoding='utf-8', errors='strict', newline='') as f:
                reader = csv.DictReader(f)

                for row in reader:
                    total_rows_read += 1
                    if row_limit and total_rows_read > row_limit:
                        break

                    raw_name = row.get('product_name', '')
                    product_name = raw_name.strip() if raw_name else ''

                    # Validate required medicine_name
                    if not product_name:
                        malformed_rows_rejected += 1
                        continue

                    raw_salt = row.get('salt_composition', '')
                    salt_composition = raw_salt.strip() if raw_salt else ''

                    raw_mfr = row.get('product_manufactured', '')
                    manufacturer = raw_mfr.strip() if raw_mfr else ''

                    # Sanitize and enforce model constraints identically to stored record
                    clean_name = product_name[:255].strip()
                    clean_comp = salt_composition or None
                    clean_mfr = manufacturer[:200].strip() if manufacturer else None

                    raw_price = row.get('product_price', '')
                    parsed_price = parse_price(raw_price)

                    # Compute composite key using exact sanitized values
                    comp_key = get_composite_key(clean_name, clean_comp, clean_mfr)

                    if comp_key in candidates:
                        duplicate_rows_skipped += 1
                        # Price resolution on duplicate: if candidate has no MRP but duplicate row has valid MRP, update
                        if candidates[comp_key]['mrp'] is None and parsed_price is not None:
                            candidates[comp_key]['mrp'] = parsed_price
                    else:
                        dosage_form = extract_dosage_form(clean_name)
                        strength = extract_strength(clean_comp, clean_name)
                        pack_size = extract_pack_size(clean_name)

                        candidates[comp_key] = {
                            'medicine_name': clean_name,
                            'composition': clean_comp,
                            'manufacturer': clean_mfr,
                            'dosage_form': dosage_form,
                            'strength': strength,
                            'pack_size': pack_size,
                            'mrp': parsed_price,
                        }
        except UnicodeDecodeError as exc:
            line_info = getattr(reader, 'line_num', total_rows_read + 1) if 'reader' in locals() else 'unknown'
            raise CommandError(
                f"Encoding error reading '{csv_file_path}' (encoding: {exc.encoding}) "
                f"at line ~{line_info}, byte position {exc.start}-{exc.end}: {exc.reason}. "
                "The CSV contains non-UTF-8 characters. Please ensure the file is strictly UTF-8 encoded."
            ) from exc

        elapsed_read = time.time() - start_time
        self.stdout.write(f"Processed {total_rows_read} rows in {elapsed_read:.2f} seconds.")

        # Step 3: Categorize unique candidates against database
        new_medicines_to_insert = []
        already_in_db_count = 0

        for key, candidate in candidates.items():
            if key in existing_db_keys:
                already_in_db_count += 1
            else:
                new_medicines_to_insert.append(
                    Medicine(
                        medicine_name=candidate['medicine_name'],
                        composition=candidate['composition'],
                        manufacturer=candidate['manufacturer'],
                        dosage_form=candidate['dosage_form'],
                        strength=candidate['strength'],
                        pack_size=candidate['pack_size'],
                        mrp=candidate['mrp'],
                    )
                )

        # Candidate quality metrics
        candidates_with_mrp = sum(1 for c in candidates.values() if c['mrp'] is not None)
        candidates_with_dosage = sum(1 for c in candidates.values() if c['dosage_form'] is not None)
        candidates_with_strength = sum(1 for c in candidates.values() if c['strength'] is not None)
        candidates_with_pack = sum(1 for c in candidates.values() if c['pack_size'] is not None)

        # Step 4: Display Summary Table
        self.stdout.write("\n" + "=" * 55)
        self.stdout.write(self.style.MIGRATE_LABEL("DATASET IMPORT AUDIT & SUMMARY REPORT"))
        self.stdout.write("=" * 55)
        self.stdout.write(f"Total CSV Rows Processed:       {total_rows_read:>8,}")
        self.stdout.write(f"Malformed / Blank Rows:         {malformed_rows_rejected:>8,}")
        self.stdout.write(f"Duplicate CSV Rows Skipped:     {duplicate_rows_skipped:>8,}")
        self.stdout.write(f"Unique Medicine Candidates:     {len(candidates):>8,}")
        self.stdout.write(f"Already in Database (Skipped):  {already_in_db_count:>8,}")
        self.stdout.write(f"New Medicines to Insert:        {len(new_medicines_to_insert):>8,}")
        self.stdout.write("-" * 55)
        self.stdout.write("Candidate Attribute Coverage:")
        self.stdout.write(f" - Valid MRP Price:             {candidates_with_mrp:>8,} ({candidates_with_mrp / (len(candidates) or 1) * 100:.1f}%)")
        self.stdout.write(f" - Extracted Dosage Form:       {candidates_with_dosage:>8,} ({candidates_with_dosage / (len(candidates) or 1) * 100:.1f}%)")
        self.stdout.write(f" - Extracted Strength:          {candidates_with_strength:>8,} ({candidates_with_strength / (len(candidates) or 1) * 100:.1f}%)")
        self.stdout.write(f" - Extracted Pack Size:         {candidates_with_pack:>8,} ({candidates_with_pack / (len(candidates) or 1) * 100:.1f}%)")
        self.stdout.write("=" * 55)

        # Step 5: Execution or Dry-Run handling
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n[DRY RUN] Simulation complete. No database records were created or modified."
                )
            )
            return

        # Live execution mode
        if not new_medicines_to_insert:
            self.stdout.write(self.style.WARNING("\nNo new medicines to insert. Database is already up to date."))
            return

        self.stdout.write(f"\nWriting {len(new_medicines_to_insert):,} records to database in batches of {batch_size}...")

        total_inserted = 0
        with transaction.atomic():
            for i in range(0, len(new_medicines_to_insert), batch_size):
                batch = new_medicines_to_insert[i:i + batch_size]
                Medicine.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
                total_inserted += len(batch)
                self.stdout.write(f"  Inserted batch {i // batch_size + 1}: {total_inserted:,} / {len(new_medicines_to_insert):,} records")

        total_elapsed = time.time() - start_time
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuccessfully imported {total_inserted:,} new medicine records into Master Medicine Catalogue in {total_elapsed:.2f}s."
            )
        )

