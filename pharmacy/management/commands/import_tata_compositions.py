import csv
import os
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.core.management.base import BaseCommand
from pharmacy.models import MedicineComposition


class Command(BaseCommand):
    help = 'Imports medicine composition dataset from data/tata_1mg_Medicine_data.csv into MedicineComposition model.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            default=os.path.join(settings.BASE_DIR, 'data', 'tata_1mg_Medicine_data.csv'),
            help='Path to the Tata 1mg CSV dataset'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit number of records to import (useful for quick verification)'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing MedicineComposition records before importing'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=3000,
            help='Batch size for bulk_create'
        )

    def handle(self, *args, **options):
        csv_path = options['file']
        limit = options['limit']
        clear_first = options['clear']
        batch_size = options['batch_size']

        if not os.path.exists(csv_path):
            self.stderr.write(self.style.ERROR(f"File not found: {csv_path}"))
            return

        if clear_first:
            self.stdout.write("Clearing existing MedicineComposition records...")
            deleted_count, _ = MedicineComposition.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Cleared {deleted_count} existing records."))

        self.stdout.write(self.style.SUCCESS(f"Starting import from: {csv_path}"))

        batch = []
        total_created = 0
        total_processed = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                name = (row.get('Name') or '').strip()
                salt = (row.get('Salt_Composition') or '').strip()

                if not name or not salt:
                    continue

                raw_mrp = (row.get('MRP') or '').strip().replace('₹', '').replace(',', '').strip()
                mrp_val = None
                if raw_mrp:
                    try:
                        mrp_val = Decimal(raw_mrp)
                    except (InvalidOperation, ValueError):
                        mrp_val = None

                quantity = (row.get('Quantity') or '').strip() or None
                manufacturer = (row.get('Manufacturer') or '').strip() or None
                image_url = (row.get('Image_URL') or '').strip() or None

                obj = MedicineComposition(
                    name=name,
                    mrp=mrp_val,
                    quantity=quantity,
                    manufacturer=manufacturer,
                    salt_composition=salt,
                    image_url=image_url
                )
                batch.append(obj)
                total_processed += 1

                if len(batch) >= batch_size:
                    MedicineComposition.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
                    total_created += len(batch)
                    batch = []
                    if total_created % 30000 == 0 or total_created < 30000 and total_created % 10000 == 0:
                        self.stdout.write(f"Processed {total_processed} rows, imported {total_created} compositions...")

                if limit and total_processed >= limit:
                    break

            if batch:
                MedicineComposition.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
                total_created += len(batch)

        self.stdout.write(self.style.SUCCESS(
            f"Successfully finished import! Total rows processed: {total_processed}, total imported: {total_created}."
        ))

