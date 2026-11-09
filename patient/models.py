from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models


class PatientProfile(models.Model):
    GENDER_CHOICES = (
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Other', 'Other'),
    )

    BLOOD_GROUP_CHOICES = (
        ('A+', 'A+'),
        ('A-', 'A-'),
        ('B+', 'B+'),
        ('B-', 'B-'),
        ('AB+', 'AB+'),
        ('AB-', 'AB-'),
        ('O+', 'O+'),
        ('O-', 'O-'),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='patient_profile'
    )
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    profile_photo = models.ImageField(upload_to='patient/profile_photos/', blank=True, null=True)
    blood_group = models.CharField(max_length=5, choices=BLOOD_GROUP_CHOICES, blank=True, null=True)
    allergies = models.TextField(blank=True, null=True)
    current_latitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    current_longitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    current_location_address = models.CharField(max_length=255, blank=True, null=True)
    location_updated_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.user.username


class Prescription(models.Model):
    STATUS_CHOICES = (
        ('UPLOADED', 'Uploaded'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    )

    patient = models.ForeignKey(
        PatientProfile,
        on_delete=models.CASCADE,
        related_name='prescriptions'
    )
    prescription_image = models.FileField(upload_to='patient/prescriptions/')
    extracted_text = models.TextField(blank=True, null=True)
    extracted_data = models.JSONField(blank=True, null=True, default=dict)
    status = models.CharField(
        max_length=15,
        choices=STATUS_CHOICES,
        default='UPLOADED'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.patient.user.username} - {self.uploaded_at}"


class PrescriptionMedicine(models.Model):
    SOURCE_CHOICES = (
        ('MANUAL', 'Manual Entry'),
        ('AI_CONFIRMED', 'AI Confirmed'),
    )

    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.CASCADE,
        related_name='medicines'
    )
    medicine_name = models.CharField(max_length=255)
    strength = models.CharField(max_length=100, blank=True, null=True)
    dosage = models.CharField(max_length=255, blank=True, null=True)
    duration = models.CharField(max_length=100, blank=True, null=True)
    quantity = models.CharField(max_length=100, blank=True, null=True)
    frequency = models.CharField(max_length=100, blank=True, null=True)
    instructions = models.TextField(blank=True, null=True)
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default='MANUAL'
    )
    extracted_index = models.PositiveIntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['prescription', 'extracted_index'],
                name='unique_prescription_extracted_medicine'
            )
        ]

    def __str__(self):
        return self.medicine_name


class SavedPharmacy(models.Model):
    patient = models.ForeignKey(
        PatientProfile,
        on_delete=models.CASCADE,
        related_name='saved_pharmacies'
    )
    # MedAccess registered pharmacy relation (nullable for public OSM records)
    pharmacy = models.ForeignKey(
        'pharmacy.PharmacyProfile',
        on_delete=models.CASCADE,
        related_name='saved_by_patients',
        null=True,
        blank=True
    )
    # OpenStreetMap public listing snapshot fields
    osm_type = models.CharField(max_length=10, blank=True, null=True)
    osm_id = models.BigIntegerField(blank=True, null=True)
    pharmacy_name = models.CharField(max_length=255, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    phone_number = models.CharField(max_length=50, blank=True, null=True)

    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['patient', 'pharmacy'],
                name='unique_patient_saved_pharmacy'
            ),
            models.UniqueConstraint(
                fields=['patient', 'osm_type', 'osm_id'],
                name='unique_patient_saved_osm_pharmacy'
            ),
        ]

    @property
    def is_medaccess(self):
        return self.pharmacy_id is not None

    @property
    def is_osm(self):
        return self.pharmacy_id is None

    @property
    def display_name(self):
        if self.pharmacy:
            return self.pharmacy.pharmacy_name
        return self.pharmacy_name or 'Public Pharmacy'

    @property
    def display_source(self):
        return 'MedAccess Verified' if self.pharmacy else 'OpenStreetMap Listing'

    @property
    def display_address(self):
        if self.pharmacy:
            parts = [self.pharmacy.address, self.pharmacy.city, self.pharmacy.state]
            full_addr = ', '.join([p.strip() for p in parts if p and p.strip()])
            if self.pharmacy.pincode:
                full_addr += f" - {self.pharmacy.pincode}"
            return full_addr
        return self.address or 'Address not listed in OpenStreetMap'

    @property
    def display_phone(self):
        if self.pharmacy:
            return self.pharmacy.phone_number or ''
        return self.phone_number or ''

    @property
    def display_latitude(self):
        if self.pharmacy:
            return self.pharmacy.latitude
        return self.latitude

    @property
    def display_longitude(self):
        if self.pharmacy:
            return self.pharmacy.longitude
        return self.longitude

    def __str__(self):
        return f"{self.patient.user.username} - {self.display_name} ({self.display_source})"


class ChatHistory(models.Model):
    patient = models.ForeignKey(
        PatientProfile,
        on_delete=models.CASCADE,
        related_name='chat_history'
    )
    message = models.TextField()
    response = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.patient.user.username} - {self.created_at}"


class PrescriptionRequest(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
        ('PROCESSING', 'Processing'),
        ('READY_FOR_PICKUP', 'Ready for Pickup'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    )

    AVAILABILITY_CHOICES = (
        ('FULLY_AVAILABLE', 'Fully Available'),
        ('PARTIALLY_AVAILABLE', 'Partially Available'),
        ('OUT_OF_STOCK', 'Out of Stock'),
    )

    ACTIVE_STATUSES = ('PENDING', 'ACCEPTED', 'PROCESSING', 'READY_FOR_PICKUP')
    TERMINAL_STATUSES = ('REJECTED', 'COMPLETED', 'CANCELLED')

    patient = models.ForeignKey(
        PatientProfile,
        on_delete=models.CASCADE,
        related_name='prescription_requests'
    )
    pharmacy = models.ForeignKey(
        'pharmacy.PharmacyProfile',
        on_delete=models.CASCADE,
        related_name='received_prescription_requests'
    )
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.PROTECT,
        related_name='requests'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING',
        db_index=True
    )
    initial_availability_status = models.CharField(
        max_length=25,
        choices=AVAILABILITY_CHOICES,
        default='PARTIALLY_AVAILABLE'
    )
    patient_notes = models.TextField(blank=True, null=True)
    pharmacy_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', '-created_at'], name='patient_req_patient_idx'),
            models.Index(fields=['pharmacy', 'status'], name='patient_req_pharma_stat_idx'),
            models.Index(fields=['prescription', 'status'], name='patient_req_rx_stat_idx'),
        ]

    def __str__(self):
        return f"Request #{self.id} - {self.patient.user.username} to {self.pharmacy.pharmacy_name} ({self.status})"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES


class PrescriptionRequestItem(models.Model):
    MATCH_STATUS_CHOICES = (
        ('MATCHED', 'Catalogue Matched'),
        ('AMBIGUOUS', 'Ambiguous Match'),
        ('UNMAPPED', 'Unmapped / Not Found'),
    )

    AVAILABILITY_STATUS_CHOICES = (
        ('FULLY_AVAILABLE', 'Fully Available'),
        ('PARTIALLY_AVAILABLE', 'Partially Available'),
        ('IN_STOCK', 'In Stock'),
        ('OUT_OF_STOCK', 'Out of Stock'),
        ('NOT_STOCKED', 'Not Stocked'),
        ('AMBIGUOUS', 'Ambiguous'),
    )

    request = models.ForeignKey(
        PrescriptionRequest,
        on_delete=models.CASCADE,
        related_name='items'
    )
    prescription_medicine = models.ForeignKey(
        PrescriptionMedicine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='request_items'
    )
    matched_catalogue_medicine = models.ForeignKey(
        'pharmacy.Medicine',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prescription_request_items'
    )

    # Immutable historical snapshots of medicine details
    prescribed_name_snapshot = models.CharField(max_length=255)
    prescribed_strength_snapshot = models.CharField(max_length=100, blank=True, null=True)
    prescribed_dosage_snapshot = models.CharField(max_length=255, blank=True, null=True)
    prescribed_frequency_snapshot = models.CharField(max_length=100, blank=True, null=True)
    prescribed_duration_snapshot = models.CharField(max_length=100, blank=True, null=True)
    prescribed_quantity_snapshot = models.CharField(max_length=100, blank=True, null=True)
    prescribed_instructions_snapshot = models.TextField(blank=True, null=True)

    match_status = models.CharField(max_length=20, choices=MATCH_STATUS_CHOICES)
    availability_status = models.CharField(max_length=25, choices=AVAILABILITY_STATUS_CHOICES)

    required_quantity = models.PositiveIntegerField(null=True, blank=True)
    required_quantity_note = models.CharField(max_length=255, blank=True)
    available_quantity_snapshot = models.PositiveIntegerField(default=0)
    explanation = models.TextField(blank=True)

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['request', 'id'], name='patient_reqitem_req_idx'),
        ]

    def __str__(self):
        return f"{self.prescribed_name_snapshot} ({self.availability_status})"


class Notification(models.Model):
    NOTIFICATION_TYPE_CHOICES = (
        ('REQUEST_SUBMITTED', 'Request Submitted'),
        ('REQUEST_ACCEPTED', 'Request Accepted'),
        ('REQUEST_REJECTED', 'Request Declined'),
        ('REQUEST_PROCESSING', 'Order Processing'),
        ('REQUEST_READY', 'Ready for Pickup'),
        ('REQUEST_COMPLETED', 'Order Completed'),
        ('REQUEST_CANCELLED', 'Request Cancelled'),
        ('NEW_MESSAGE', 'New Message'),
    )

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        db_index=True
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acted_notifications'
    )
    prescription_request = models.ForeignKey(
        PrescriptionRequest,
        on_delete=models.CASCADE,
        related_name='notifications'
    )
    notification_type = models.CharField(
        max_length=30,
        choices=NOTIFICATION_TYPE_CHOICES,
        db_index=True
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at'], name='patient_notif_recip_read_idx'),
            models.Index(fields=['prescription_request', 'notification_type'], name='patient_notif_req_type_idx'),
        ]

    def __str__(self):
        return f"Notification #{self.id} to {self.recipient.username}: {self.title}"

    def clean(self):
        super().clean()
        if self.title is not None and not self.title.strip():
            raise ValidationError({'title': 'Title cannot be empty.'})
        if self.message is not None and not self.message.strip():
            raise ValidationError({'message': 'Message cannot be empty.'})


class RequestMessage(models.Model):
    request = models.ForeignKey(
        PrescriptionRequest,
        on_delete=models.CASCADE,
        related_name='messages',
        db_index=True
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_request_messages'
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_request_messages'
    )
    message_text = models.TextField(validators=[MaxLengthValidator(1000)])
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['request', 'created_at'], name='patient_reqmsg_req_time_idx'),
            models.Index(fields=['recipient', 'is_read'], name='patient_reqmsg_recip_read_idx'),
        ]

    def __str__(self):
        return f"Message #{self.id} (Req #{self.request_id}) from {self.sender.username} to {self.recipient.username}"

    def clean(self):
        super().clean()

        # Validate content
        if not self.message_text or not self.message_text.strip():
            raise ValidationError({'message_text': 'Message content cannot be empty or whitespace-only.'})
        if len(self.message_text) > 1000:
            raise ValidationError({'message_text': 'Message content cannot exceed 1000 characters.'})

        # Validate sender != recipient
        if self.sender_id and self.recipient_id and self.sender_id == self.recipient_id:
            raise ValidationError({'recipient': 'Sender and recipient cannot be the same user.'})

        # Validate request participants
        if hasattr(self, 'request') and self.request:
            patient_user = getattr(self.request.patient, 'user', None)
            pharmacy_user = getattr(self.request.pharmacy, 'user', None)

            if patient_user and pharmacy_user:
                allowed_ids = {patient_user.id, pharmacy_user.id}

                if self.sender_id and self.sender_id not in allowed_ids:
                    raise ValidationError({'sender': 'Sender must be a participant in this prescription request.'})

                if self.recipient_id and self.recipient_id not in allowed_ids:
                    raise ValidationError({'recipient': 'Recipient must be a participant in this prescription request.'})

                if self.sender_id == patient_user.id and self.recipient_id != pharmacy_user.id:
                    raise ValidationError({'recipient': "A message sent by the patient must be addressed to the request's pharmacy."})

                if self.sender_id == pharmacy_user.id and self.recipient_id != patient_user.id:
                    raise ValidationError({'recipient': "A message sent by the pharmacy must be addressed to the request's patient."})


