"""One-time conversion of POC plaintext columns to Fernet ciphertext."""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mindcare.models import ChatMessage, JournalEntry, SafetyAlert, SafetyPlan, TrustedContact


class Command(BaseCommand):
    help = 'Encrypt legacy plaintext in sensitive MindCare columns using MINDCARE_DATA_ENCRYPTION_KEY.'

    def handle(self, *args, **options):
        try:
            cipher = Fernet(settings.MINDCARE_DATA_ENCRYPTION_KEY)
        except (ValueError, TypeError):
            raise CommandError('Set a valid MINDCARE_DATA_ENCRYPTION_KEY first.')
        fields = (
            (JournalEntry, 'content'),
            (ChatMessage, 'content'),
            (TrustedContact, 'contact_value_encrypted'),
            (SafetyPlan, 'personal_notes'),
            (SafetyAlert, 'reason'),
        )
        converted = 0
        with transaction.atomic():
            for model, field in fields:
                for row in model.objects.select_for_update().only('id', field).iterator():
                    value = getattr(row, field)
                    if not value:
                        continue
                    if value.startswith('gAAAA'):
                        try:
                            cipher.decrypt(value.encode())
                        except InvalidToken:
                            raise CommandError(f'{model.__name__} {row.pk} has ciphertext encrypted with another key.')
                        continue
                    model.objects.filter(pk=row.pk).update(**{field: cipher.encrypt(value.encode()).decode()})
                    converted += 1
        self.stdout.write(self.style.SUCCESS(f'Encrypted {converted} legacy values.'))
