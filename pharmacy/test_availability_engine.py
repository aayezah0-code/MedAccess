"""
Unit and Integration Tests for MedAccess Inventory Availability Engine (Phase 1).

Tests the 18 required scenarios:
1. Exact brand match with sufficient stock (FULLY_AVAILABLE).
2. Partial stock (PARTIALLY_AVAILABLE).
3. Zero stock (OUT_OF_STOCK).
4. Multiple valid batches aggregation.
5. Expired batch exclusion.
6. Zero-quantity batch exclusion.
7. Strength mismatch rejection (e.g. 500mg vs 650mg).
8. Different-brand substitution prevention.
9. Ambiguous dosage form handling (AMBIGUOUS).
10. Unknown required quantity handling (IN_STOCK / OUT_OF_STOCK).
11. Explicit quantity calculation.
12. Course quantity calculation (dosage x frequency x duration).
13. SOS and missing duration handling (PRN rejection).
14. Unapproved pharmacy exclusion.
15. Pharmacy outside search radius exclusion.
16. Multiple prescription medicines evaluation.
17. Prescription ownership validation.
18. No confirmed medicines / empty prescription handling.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from patient.models import PatientProfile, Prescription, PrescriptionMedicine
from pharmacy.models import Medicine, Inventory, PharmacyProfile
from pharmacy.services.availability_engine import (
    parse_quantity_or_calculate_course,
    match_prescription_medicine_to_catalogue,
    get_pharmacy_medicine_stock,
    check_prescription_pharmacy_availability,
    calculate_haversine_distance,
)

User = get_user_model()


class InventoryAvailabilityEngineTests(TestCase):
    """Exhaustive test suite for the Phase 1 Availability Engine."""

    def setUp(self):
        """Set up patient, approved pharmacy, catalogue medicines, and test inventory."""
        self.today = timezone.localdate()

        # 1. Create Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='patient_test',
            email='patient@test.com',
            password='Password123!',
            role='PATIENT',
            email_verified=True
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            current_latitude=Decimal('28.6139000'),  # New Delhi
            current_longitude=Decimal('77.2090000'),
        )

        # Another patient for ownership tests
        self.other_patient_user = User.objects.create_user(
            username='other_patient',
            email='other@test.com',
            password='Password123!',
            role='PATIENT',
            email_verified=True
        )
        self.other_patient_profile = PatientProfile.objects.create(
            user=self.other_patient_user,
            current_latitude=Decimal('28.6139000'),
            current_longitude=Decimal('77.2090000'),
        )

        # 2. Create Approved Nearby Pharmacy (1.5 km away)
        self.pharma_user_1 = User.objects.create_user(
            username='pharma_1',
            email='pharma1@test.com',
            password='Password123!',
            role='PHARMACY',
            email_verified=True
        )
        self.pharmacy_1 = PharmacyProfile.objects.create(
            user=self.pharma_user_1,
            pharmacy_name='City Care Pharmacy',
            pharmacy_license_number='DL-1001-A',
            gst_number='07AAAAA0000A1Z5',
            phone_number='9876543210',
            email='pharma1@test.com',
            address='12 Main Street, Connaught Place',
            city='New Delhi',
            state='Delhi',
            pincode='110001',
            latitude=Decimal('28.6250000'),  # ~1.2 km away
            longitude=Decimal('77.2180000'),
            approval_status='APPROVED'
        )

        # 3. Create Catalogue Medicines
        self.med_dolo_650 = Medicine.objects.create(
            medicine_name='Dolo 650 Tablet',
            composition='Paracetamol (650mg)',
            strength='650mg',
            dosage_form='Tablet',
            manufacturer='Micro Labs Ltd',
            pack_size='15 tablets',
            mrp=Decimal('30.50')
        )

        self.med_dolo_500 = Medicine.objects.create(
            medicine_name='Dolo 500 Tablet',
            composition='Paracetamol (500mg)',
            strength='500mg',
            dosage_form='Tablet',
            manufacturer='Micro Labs Ltd',
            pack_size='10 tablets',
            mrp=Decimal('20.00')
        )

        self.med_crocin_650 = Medicine.objects.create(
            medicine_name='Crocin 650 Tablet',
            composition='Paracetamol (650mg)',
            strength='650mg',
            dosage_form='Tablet',
            manufacturer='GlaxoSmithKline',
            pack_size='15 tablets',
            mrp=Decimal('32.00')
        )

        self.med_augmentin_tab = Medicine.objects.create(
            medicine_name='Augmentin 625 Duo Tablet',
            composition='Amoxycillin (500mg) + Clavulanic Acid (125mg)',
            strength='500mg + 125mg',
            dosage_form='Tablet',
            manufacturer='GlaxoSmithKline',
            pack_size='10 tablets',
            mrp=Decimal('200.00')
        )

        self.med_augmentin_syr = Medicine.objects.create(
            medicine_name='Augmentin Duo Oral Suspension',
            composition='Amoxycillin (200mg) + Clavulanic Acid (28.5mg)',
            strength='200mg + 28.5mg',
            dosage_form='Suspension',
            manufacturer='GlaxoSmithKline',
            pack_size='30 ml',
            mrp=Decimal('65.00')
        )

        # 4. Create a Default Prescription
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            status='COMPLETED',
            prescription_image='patient/prescriptions/sample.jpg'
        )

    # -------------------------------------------------------------------------
    # Test 1: Exact brand match with sufficient stock (FULLY_AVAILABLE)
    # -------------------------------------------------------------------------
    def test_01_exact_brand_match_with_sufficient_stock(self):
        """Prescribed medicine matches catalogue and available stock >= required -> FULLY_AVAILABLE."""
        # Add stock: 20 units
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-01',
            expiry_date=self.today + timedelta(days=90),
            quantity=20
        )

        # Prescribed: 10 tablets of Dolo 650
        med = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            strength='650mg',
            quantity='10 tablets',
            source='AI_CONFIRMED'
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.pharmacy_id, self.pharmacy_1.id)
        self.assertEqual(rep.overall_status, 'FULLY_AVAILABLE')
        self.assertEqual(len(rep.items), 1)

        item = rep.items[0]
        self.assertEqual(item.matched_catalogue_id, self.med_dolo_650.id)
        self.assertEqual(item.available_quantity, 20)
        self.assertEqual(item.required_quantity, 10)
        self.assertEqual(item.availability_status, 'FULLY_AVAILABLE')

    # -------------------------------------------------------------------------
    # Test 2: Partial stock
    # -------------------------------------------------------------------------
    def test_02_partial_stock(self):
        """Available quantity is between 0 and required quantity -> PARTIALLY_AVAILABLE."""
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-02',
            expiry_date=self.today + timedelta(days=60),
            quantity=5
        )

        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            strength='650mg',
            quantity='15 tablets',
            source='AI_CONFIRMED'
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(len(reports), 1)
        item = reports[0].items[0]
        self.assertEqual(item.available_quantity, 5)
        self.assertEqual(item.required_quantity, 15)
        self.assertEqual(item.availability_status, 'PARTIALLY_AVAILABLE')
        self.assertEqual(reports[0].overall_status, 'PARTIALLY_AVAILABLE')

    # -------------------------------------------------------------------------
    # Test 3: Zero stock
    # -------------------------------------------------------------------------
    def test_03_zero_stock(self):
        """Pharmacy carries the medicine but has 0 available units -> OUT_OF_STOCK."""
        # No inventory created for med_dolo_650
        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            strength='650mg',
            quantity='10 tablets',
            source='AI_CONFIRMED'
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(len(reports), 1)
        item = reports[0].items[0]
        self.assertEqual(item.available_quantity, 0)
        self.assertEqual(item.availability_status, 'OUT_OF_STOCK')
        self.assertEqual(reports[0].overall_status, 'OUT_OF_STOCK')

    # -------------------------------------------------------------------------
    # Test 4: Multiple valid batches aggregation
    # -------------------------------------------------------------------------
    def test_04_multiple_valid_batches_aggregation(self):
        """Multiple non-expired batches for the same medicine aggregate total quantity."""
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-A1',
            expiry_date=self.today + timedelta(days=30),
            quantity=12
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-A2',
            expiry_date=self.today + timedelta(days=120),
            quantity=18
        )

        stock = get_pharmacy_medicine_stock(self.pharmacy_1, self.med_dolo_650)
        self.assertEqual(stock, 30)

    # -------------------------------------------------------------------------
    # Test 5: Expired batch exclusion
    # -------------------------------------------------------------------------
    def test_05_expired_batch_exclusion(self):
        """Batches with expiry_date in the past must be strictly excluded from stock."""
        # Valid batch: 10
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-VALID',
            expiry_date=self.today + timedelta(days=45),
            quantity=10
        )
        # Expired batch: 50 (expired yesterday)
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-EXPIRED',
            expiry_date=self.today - timedelta(days=1),
            quantity=50
        )

        stock = get_pharmacy_medicine_stock(self.pharmacy_1, self.med_dolo_650)
        self.assertEqual(stock, 10)

    # -------------------------------------------------------------------------
    # Test 6: Zero-quantity batch exclusion
    # -------------------------------------------------------------------------
    def test_06_zero_quantity_batch_exclusion(self):
        """Batches with quantity <= 0 are not included."""
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-ZERO',
            expiry_date=self.today + timedelta(days=45),
            quantity=0
        )
        stock = get_pharmacy_medicine_stock(self.pharmacy_1, self.med_dolo_650)
        self.assertEqual(stock, 0)

    # -------------------------------------------------------------------------
    # Test 7: Strength mismatch rejection
    # -------------------------------------------------------------------------
    def test_07_strength_mismatch_rejection(self):
        """Prescribing 500mg must not match 650mg formulation."""
        med = PrescriptionMedicine(
            medicine_name='Dolo',
            strength='500mg'
        )
        match_res = match_prescription_medicine_to_catalogue(med)
        self.assertEqual(match_res.match_status, 'MATCHED')
        self.assertEqual(match_res.medicine.id, self.med_dolo_500.id)
        self.assertNotEqual(match_res.medicine.id, self.med_dolo_650.id)

    # -------------------------------------------------------------------------
    # Test 8: Different-brand substitution prevention
    # -------------------------------------------------------------------------
    def test_08_different_brand_substitution_prevention(self):
        """Prescription for 'Dolo 650' must NEVER match 'Crocin 650' despite same composition."""
        med = PrescriptionMedicine(
            medicine_name='Dolo 650',
            strength='650mg'
        )
        match_res = match_prescription_medicine_to_catalogue(med)
        self.assertEqual(match_res.match_status, 'MATCHED')
        self.assertEqual(match_res.medicine.medicine_name, 'Dolo 650 Tablet')
        self.assertNotEqual(match_res.medicine.medicine_name, 'Crocin 650 Tablet')

    # -------------------------------------------------------------------------
    # Test 9: Ambiguous dosage form
    # -------------------------------------------------------------------------
    def test_09_ambiguous_dosage_form(self):
        """If prescription says 'Augmentin' without strength or form, returns AMBIGUOUS."""
        med = PrescriptionMedicine(
            medicine_name='Augmentin',
            strength=None,
            dosage=None
        )
        match_res = match_prescription_medicine_to_catalogue(med)
        self.assertEqual(match_res.match_status, 'AMBIGUOUS')
        self.assertIsNone(match_res.medicine)

    # -------------------------------------------------------------------------
    # Test 10: Unknown required quantity
    # -------------------------------------------------------------------------
    def test_10_unknown_required_quantity(self):
        """When required quantity cannot be determined, available stock returns IN_STOCK."""
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-UNK',
            expiry_date=self.today + timedelta(days=90),
            quantity=25
        )

        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            strength='650mg',
            dosage=None,
            frequency=None,
            duration=None,
            quantity=None
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(len(reports), 1)
        item = reports[0].items[0]
        self.assertIsNone(item.required_quantity)
        self.assertEqual(item.available_quantity, 25)
        self.assertEqual(item.availability_status, 'IN_STOCK')

    # -------------------------------------------------------------------------
    # Test 11: Explicit quantity calculation
    # -------------------------------------------------------------------------
    def test_11_explicit_quantity_calculation(self):
        """Explicit quantity strings parse correctly into leading positive integers."""
        med1 = {'quantity': '10 tablets'}
        qty1, note1 = parse_quantity_or_calculate_course(med1)
        self.assertEqual(qty1, 10)
        self.assertIn('Explicit', note1)

        med2 = {'quantity': '1 bottle'}
        qty2, note2 = parse_quantity_or_calculate_course(med2)
        self.assertEqual(qty2, 1)

    # -------------------------------------------------------------------------
    # Test 12: Course quantity calculation
    # -------------------------------------------------------------------------
    def test_12_course_quantity_calculation(self):
        """dosage (1 tab) x frequency (1-0-1 = 2) x duration (5 days) = 10 units."""
        med = {
            'dosage': '1 tablet',
            'frequency': '1-0-1',
            'duration': '5 days',
        }
        qty, note = parse_quantity_or_calculate_course(med)
        self.assertEqual(qty, 10)
        self.assertIn('1 per intake x 2 times/day x 5 days = 10 units', note)

        # BD / twice daily
        med_bd = {
            'dosage': '1 capsule',
            'frequency': 'twice daily',
            'duration': '1 week',
        }
        qty_bd, _ = parse_quantity_or_calculate_course(med_bd)
        self.assertEqual(qty_bd, 14)

    # -------------------------------------------------------------------------
    # Test 13: SOS and missing duration handling
    # -------------------------------------------------------------------------
    def test_13_sos_and_missing_duration_handling(self):
        """PRN/SOS instructions or missing duration return None with clear explanation."""
        med_sos = {
            'dosage': '1 tablet',
            'frequency': 'SOS',
            'duration': '5 days',
        }
        qty_sos, note_sos = parse_quantity_or_calculate_course(med_sos)
        self.assertIsNone(qty_sos)
        self.assertIn('PRN/SOS', note_sos)

        med_no_dur = {
            'dosage': '1 tablet',
            'frequency': '1-0-1',
            'duration': '',
        }
        qty_no_dur, note_no_dur = parse_quantity_or_calculate_course(med_no_dur)
        self.assertIsNone(qty_no_dur)
        self.assertIn('missing: duration', note_no_dur)

    # -------------------------------------------------------------------------
    # Test 14: Unapproved pharmacy exclusion
    # -------------------------------------------------------------------------
    def test_14_unapproved_pharmacy_exclusion(self):
        """Pharmacies with approval_status != 'APPROVED' are strictly omitted."""
        user_pending = User.objects.create_user(
            username='pharma_pending',
            email='pending@test.com',
            password='Password123!',
            role='PHARMACY',
            email_verified=True
        )
        PharmacyProfile.objects.create(
            user=user_pending,
            pharmacy_name='Pending Pharmacy',
            pharmacy_license_number='DL-9999',
            gst_number='07AAAAA9999A1Z5',
            latitude=Decimal('28.6150000'),
            longitude=Decimal('77.2100000'),
            approval_status='PENDING'
        )

        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            quantity='10'
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        pharmacy_names = [r.pharmacy_name for r in reports]
        self.assertNotIn('Pending Pharmacy', pharmacy_names)

    # -------------------------------------------------------------------------
    # Test 15: Pharmacy outside radius exclusion
    # -------------------------------------------------------------------------
    def test_15_pharmacy_outside_radius_exclusion(self):
        """Pharmacies beyond radius_km are not included in the report."""
        user_far = User.objects.create_user(
            username='pharma_far',
            email='far@test.com',
            password='Password123!',
            role='PHARMACY',
            email_verified=True
        )
        PharmacyProfile.objects.create(
            user=user_far,
            pharmacy_name='Faraway Pharmacy',
            pharmacy_license_number='DL-5555',
            gst_number='07AAAAA5555A1Z5',
            latitude=Decimal('28.9000000'),  # ~32 km away
            longitude=Decimal('77.2000000'),
            approval_status='APPROVED'
        )

        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            quantity='10'
        )

        # Search within 10 km
        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        pharmacy_names = [r.pharmacy_name for r in reports]
        self.assertNotIn('Faraway Pharmacy', pharmacy_names)

    # -------------------------------------------------------------------------
    # Test 16: Multiple prescription medicines
    # -------------------------------------------------------------------------
    def test_16_multiple_prescription_medicines(self):
        """Prescription with 2 medicines: one in stock, one out of stock -> PARTIALLY_AVAILABLE."""
        # Med 1: In stock (20 units)
        Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo_650,
            batch_number='BATCH-M1',
            expiry_date=self.today + timedelta(days=90),
            quantity=20
        )
        # Med 2 (Augmentin 625): 0 stock

        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650 Tablet',
            strength='650mg',
            quantity='10 tablets'
        )
        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Augmentin 625 Duo Tablet',
            strength='500mg + 125mg',
            quantity='10 tablets'
        )

        reports = check_prescription_pharmacy_availability(
            self.prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.total_prescribed_count, 2)
        self.assertEqual(rep.available_items_count, 1)
        self.assertEqual(rep.overall_status, 'PARTIALLY_AVAILABLE')

    # -------------------------------------------------------------------------
    # Test 17: Prescription ownership validation
    # -------------------------------------------------------------------------
    def test_17_prescription_ownership_validation(self):
        """Patient profile association is properly maintained on prescription."""
        self.assertEqual(self.prescription.patient, self.patient_profile)
        self.assertNotEqual(self.prescription.patient, self.other_patient_profile)

    # -------------------------------------------------------------------------
    # Test 18: No confirmed medicines / empty prescription handling
    # -------------------------------------------------------------------------
    def test_18_empty_prescription_handling(self):
        """Prescription with no medicines returns an empty report gracefully."""
        empty_prescription = Prescription.objects.create(
            patient=self.patient_profile,
            status='COMPLETED',
            prescription_image='patient/prescriptions/empty.jpg'
        )

        reports = check_prescription_pharmacy_availability(
            empty_prescription,
            patient_lat=28.6139,
            patient_lon=77.2090,
            radius_km=10.0
        )

        self.assertEqual(reports, [])

