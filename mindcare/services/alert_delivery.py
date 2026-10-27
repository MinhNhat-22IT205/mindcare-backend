"""Explicit worker boundary for alert delivery; never send without current consent."""

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from mindcare.api.common import consent, decrypt
from mindcare.models import AlertDelivery


def deliver_queued_alert(alert_id, sender=None):
    sender = sender or import_string(settings.MINDCARE_ALERT_SENDER)
    for delivery in AlertDelivery.objects.select_related('alert', 'trusted_contact').filter(
            alert_id=alert_id, delivery_status='queued'):
        alert = delivery.alert
        contact = delivery.trusted_contact
        if (alert.status != 'approved' or contact is None or
                contact.user_id != alert.user_id or contact.verification_status != 'verified' or
                not consent(alert.user, f'share_alert_contact:{contact.id}')):
            delivery.delivery_status = 'cancelled'
            delivery.save(update_fields=['delivery_status'])
            continue
        address = decrypt(contact.contact_value_encrypted)
        try:
            sender(channel=delivery.channel, recipient=address,
                   message='Người liên hệ của bạn đã yêu cầu được hỗ trợ. Vui lòng chủ động liên lạc trực tiếp nếu phù hợp.')
        except Exception:
            delivery.delivery_status = 'failed'
            delivery.error_code = 'provider_error'
            delivery.save(update_fields=['delivery_status', 'error_code'])
        else:
            delivery.delivery_status = 'sent'
            delivery.sent_at = timezone.now()
            delivery.save(update_fields=['delivery_status', 'sent_at'])
