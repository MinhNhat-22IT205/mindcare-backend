from dataclasses import dataclass

from mindcare.models import ChatMessage, SafetyAlert
from mindcare.api.common import decrypt, encrypt
from mindcare.services.configuration import get_configured_callable


@dataclass(frozen=True)
class SafetySignal:
    severity: str
    reason: str


def screen_chat_message(message: ChatMessage, detector=None):
    """Screen one message immediately and persist an alert when a signal is found."""
    detector = detector or get_configured_callable('MINDCARE_SAFETY_DETECTOR')
    signal = detector(decrypt(message.content))
    if signal is None:
        return None
    if isinstance(signal, dict):
        signal = SafetySignal(**signal)
    if not isinstance(signal, SafetySignal):
        raise TypeError('Safety detector must return SafetySignal, a matching dictionary, or None.')
    open_status = 'pending_review'
    alert, _ = SafetyAlert.objects.get_or_create(
        user=message.session.user,
        trigger_message=message,
        status=open_status,
        defaults={
            'trigger_type': 'chat_signal',
            'severity': signal.severity,
            'reason': encrypt(signal.reason),
        },
    )
    return alert
