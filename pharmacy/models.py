from django.conf import settings
from django.db import models


class PharmacyProfile(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pharmacy_profile'
    )
    pharmacy_name = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=150)
    pharmacist_name = models.CharField(max_length=150)
    pharmacy_license_number = models.CharField(max_length=100)
    gst_number = models.CharField(max_length=20)
    phone_number = models.CharField(max_length=15)
    email = models.EmailField()
    address = models.TextField()
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(max_length=10)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    approval_status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='PENDING'
    )
    rejection_reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.pharmacy_name


class Medicine(models.Model):
    medicine_name = models.CharField(max_length=255)
    composition = models.TextField(blank=True, null=True)
    strength = models.CharField(max_length=100, blank=True, null=True)
    manufacturer = models.CharField(max_length=200, blank=True, null=True)
    dosage_form = models.CharField(max_length=100, blank=True, null=True)
    pack_size = models.CharField(max_length=100, blank=True, null=True)
    mrp = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    medicine_image = models.ImageField(upload_to='pharmacy/medicines/', blank=True, null=True)

    def __str__(self):
        return self.medicine_name


class Inventory(models.Model):
    pharmacy = models.ForeignKey(
        PharmacyProfile,
        on_delete=models.CASCADE,
        related_name='inventory'
    )
    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.CASCADE,
        related_name='inventory_records'
    )
    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField()
    quantity = models.PositiveIntegerField(default=0)
    unit = models.CharField(max_length=50, default='Units')
    low_stock_limit = models.PositiveIntegerField(default=10)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'medicine', 'batch_number'],
                name='unique_pharmacy_medicine_batch'
            )
        ]

    def __str__(self):
        return f"{self.pharmacy.pharmacy_name} - {self.medicine.medicine_name}"


class Feedback(models.Model):
    RATING_CHOICES = (
        (1, '1'),
        (2, '2'),
        (3, '3'),
        (4, '4'),
        (5, '5'),
    )

    patient = models.ForeignKey(
        'patient.PatientProfile',
        on_delete=models.CASCADE,
        related_name='feedbacks'
    )
    pharmacy = models.ForeignKey(
        PharmacyProfile,
        on_delete=models.CASCADE,
        related_name='feedbacks'
    )
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES)
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['patient', 'pharmacy'],
                name='unique_patient_pharmacy_feedback'
            )
        ]

    def __str__(self):
        return f"{self.patient.user.username} - {self.pharmacy.pharmacy_name}"


class MedicineComposition(models.Model):
    name = models.CharField(max_length=255, db_index=True)
    mrp = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quantity = models.CharField(max_length=150, null=True, blank=True)
    manufacturer = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    salt_composition = models.TextField()
    image_url = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['name'], name='idx_medcomp_name'),
            models.Index(fields=['manufacturer'], name='idx_medcomp_mfg'),
        ]
        verbose_name = 'Medicine Composition'
        verbose_name_plural = 'Medicine Compositions'

    def __str__(self):
        return f"{self.name} - {self.salt_composition[:50]}"

