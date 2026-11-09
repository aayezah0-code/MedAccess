import getpass
import sys
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction, models

from accounts.models import User
from accounts.decorators import get_predefined_admin_emails


class Command(BaseCommand):
    help = (
        "Provision and verify the two predefined MedAccess administrator accounts. "
        "Strictly enforces exactly two authorized admin identities."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            type=str,
            default=None,
            help="Username to use if creating a new admin account (optional)."
        )

    def prompt_password(self, email):
        """
        Securely prompts for password using Django/Python password input mechanisms.
        Never accepts passwords via command-line arguments.
        """
        while True:
            if sys.stdin.isatty():
                pwd = getpass.getpass(f"Enter password for new administrator ({email}): ")
            else:
                pwd = getpass.getpass()

            if not pwd:
                self.stderr.write(self.style.ERROR("Password cannot be blank. Please try again."))
                continue

            try:
                validate_password(pwd)
            except ValidationError as err:
                for msg in err.messages:
                    self.stderr.write(self.style.ERROR(f"Validation error: {msg}"))
                self.stderr.write(self.style.WARNING("Please choose a stronger password."))
                continue

            if sys.stdin.isatty():
                confirm_pwd = getpass.getpass("Confirm password: ")
                if pwd != confirm_pwd:
                    self.stderr.write(self.style.ERROR("Passwords do not match. Please try again."))
                    continue

            return pwd

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== MedAccess Predefined Admin Provisioning ==="))

        # 1. Load and validate predefined identities from settings
        predefined_emails = sorted(list(get_predefined_admin_emails()))
        if len(predefined_emails) != 2:
            raise CommandError(
                f"Configuration error: Expected exactly 2 predefined admin emails, but found {len(predefined_emails)}: "
                f"{predefined_emails}. Check PREDEFINED_ADMIN_EMAILS in settings/.env."
            )

        self.stdout.write(f"Authorized admin identities configured: {', '.join(predefined_emails)}")

        # 2. Check for conflicting or unauthorized admin/staff/superuser accounts in database
        conflicting_users = User.objects.filter(
            models.Q(role='ADMIN') | models.Q(is_staff=True) | models.Q(is_superuser=True)
        ).exclude(email__in=predefined_emails)

        if conflicting_users.exists():
            conflict_details = [
                f"'{u.username}' (email: '{u.email}', role: '{u.role}', staff: {u.is_staff}, superuser: {u.is_superuser})"
                for u in conflicting_users
            ]
            raise CommandError(
                f"Safety conflict: Found {conflicting_users.count()} unauthorized account(s) with administrative or staff privileges:\n"
                + "\n".join(f"  - {d}" for d in conflict_details)
                + "\nMedAccess strictly restricts admin privileges to exactly the two predefined identities. "
                "Resolve or demote these conflicting accounts manually before provisioning."
            )

        # 3. Process each predefined admin identity
        with transaction.atomic():
            for email in predefined_emails:
                existing_user = User.objects.filter(email__iexact=email).first()

                if existing_user:
                    # Account exists: check identity and role integrity
                    if existing_user.role != 'ADMIN':
                        raise CommandError(
                            f"Safety conflict: User '{existing_user.username}' exists with email '{email}', "
                            f"but has role '{existing_user.role}'. "
                            "Automatic conversion of an existing non-admin account to ADMIN is strictly forbidden. "
                            "Please resolve this account conflict manually."
                        )

                    # Ensure required admin flags are set consistently
                    updated_fields = []
                    if not existing_user.is_staff:
                        existing_user.is_staff = True
                        updated_fields.append('is_staff')
                    if not existing_user.is_superuser:
                        existing_user.is_superuser = True
                        updated_fields.append('is_superuser')
                    if not existing_user.is_active:
                        existing_user.is_active = True
                        updated_fields.append('is_active')
                    if not existing_user.email_verified:
                        existing_user.email_verified = True
                        updated_fields.append('email_verified')
                    if not existing_user.is_approved:
                        existing_user.is_approved = True
                        updated_fields.append('is_approved')

                    if updated_fields:
                        existing_user.save(update_fields=updated_fields + ['updated_at'])
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Updated flags for existing admin '{existing_user.username}' ({email}): {', '.join(updated_fields)}. "
                                "Password preserved."
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Admin account '{existing_user.username}' ({email}) is valid and verified. Password preserved."
                            )
                        )

                else:
                    # Account does not exist: create second admin account
                    self.stdout.write(self.style.WARNING(f"Admin account for '{email}' does not exist. Creating account..."))

                    suggested_username = options.get('username')
                    if not suggested_username:
                        # Derive from email prefix
                        local_part = email.split('@')[0].replace('.', '_').replace('-', '_')
                        suggested_username = local_part

                    # Ensure username is unique
                    username = suggested_username
                    counter = 1
                    while User.objects.filter(username__iexact=username).exists():
                        username = f"{suggested_username}_{counter}"
                        counter += 1

                    password = self.prompt_password(email)

                    new_admin = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password,
                        role='ADMIN',
                        is_staff=True,
                        is_superuser=True,
                        is_active=True,
                        email_verified=True,
                        is_approved=True,
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Successfully provisioned new admin '{new_admin.username}' ({email}) with secure password hash."
                        )
                    )

            # 4. Final verification: ensure strictly at most 2 admins in system
            total_admins = User.objects.filter(role='ADMIN').count()
            if total_admins > 2:
                raise CommandError(
                    f"Integrity violation: {total_admins} ADMIN accounts exist in the database. "
                    "MedAccess allows a maximum of exactly 2 admin accounts."
                )

        self.stdout.write(self.style.SUCCESS("All predefined administrator identities provisioned and verified successfully."))

