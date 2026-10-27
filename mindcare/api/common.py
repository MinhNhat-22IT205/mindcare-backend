"""Small HTTP boundary for the versioned MindCare API."""
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
import jwt

from mindcare.models import AnalysisRun, AnalysisSource, User, UserConsent


logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status, code, message=None, details=None):
        self.status = status
        self.code = code
        self.message = message or code.replace('_', ' ').capitalize()
        self.details = details or []


def fail(status, code, message=None):
    raise APIError(status, code, message)


def respond(request, data=None, status=200):
    if status == 204:
        response = HttpResponse(status=204)
    else:
        response = JsonResponse({'data': data, 'meta': {'request_id': request.request_id}}, status=status)
    response['Cache-Control'] = 'no-store'
    response['X-Request-Id'] = request.request_id
    return response


def api(methods, roles=None):
    """Check method, JWT and current database role before a view runs."""
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            supplied = request.headers.get('X-Request-Id', '')
            try:
                request.request_id = str(uuid.UUID(supplied)) if supplied else str(uuid.uuid4())
            except ValueError:
                request.request_id = str(uuid.uuid4())
            try:
                if request.method not in methods:
                    fail(405, 'METHOD_NOT_ALLOWED')
                if roles is not None:
                    request.api_user = authenticate(request)
                    if request.api_user.role not in roles:
                        fail(403, 'ROLE_FORBIDDEN')
                return view(request, *args, **kwargs)
            except APIError as error:
                response = JsonResponse({
                    'error': {'code': error.code, 'message': error.message, 'details': error.details},
                    'meta': {'request_id': request.request_id},
                }, status=error.status)
                response['Cache-Control'] = 'no-store'
                response['X-Request-Id'] = request.request_id
                return response
            except Exception:
                logger.error('MindCare API failure request_id=%s view=%s',
                             request.request_id, view.__name__)
                response = JsonResponse({
                    'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error.', 'details': []},
                    'meta': {'request_id': request.request_id},
                }, status=500)
                response['Cache-Control'] = 'no-store'
                response['X-Request-Id'] = request.request_id
                return response
        wrapped.allowed_methods = frozenset(methods)
        return wrapped
    return decorate


def body(request, allowed, required=()):
    if request.body and request.content_type != 'application/json':
        fail(422, 'VALIDATION_ERROR', 'Content-Type must be application/json.')
    if len(request.body) > 65536:
        fail(422, 'VALIDATION_ERROR', 'Request body is too large.')
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        fail(422, 'VALIDATION_ERROR', 'Expected a JSON object.')
    if not isinstance(payload, dict) or set(payload) - set(allowed) or set(required) - set(payload):
        fail(422, 'VALIDATION_ERROR', 'Request fields are missing or unsupported.')
    return payload


def text_value(value, name, max_length, *, blank=False):
    if not isinstance(value, str) or len(value) > max_length or (not blank and not value.strip()):
        fail(422, 'VALIDATION_ERROR', f'Invalid {name}.')
    return value.strip()


def choice(value, options, name='value'):
    if value not in options:
        fail(422, 'VALIDATION_ERROR', f'Invalid {name}.')
    return value


def boolean(value, name):
    if type(value) is not bool:
        fail(422, 'VALIDATION_ERROR', f'Invalid {name}.')
    return value


def parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        fail(404, 'NOT_FOUND')


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        fail(422, 'INVALID_DATE_RANGE')


def parse_datetime(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timezone.is_naive(result):
            raise ValueError
        return result
    except (TypeError, ValueError, AttributeError):
        fail(422, 'VALIDATION_ERROR', 'Expected ISO 8601 datetime with timezone.')


def valid_timezone(value):
    value = text_value(value, 'timezone', 64)
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        fail(422, 'VALIDATION_ERROR', 'Invalid IANA timezone.')
    return value


def local_today(user):
    return timezone.localtime(timezone.now(), ZoneInfo(user.timezone)).date()


def date_range(request, user, *, default_days=14, max_days=90):
    end = parse_date(request.GET['to']) if 'to' in request.GET else local_today(user)
    start = parse_date(request.GET['from']) if 'from' in request.GET else end - timedelta(days=default_days - 1)
    if start > end or end > local_today(user) or (end - start).days >= max_days:
        fail(422, 'INVALID_DATE_RANGE')
    return start, end


def page(request, queryset, serialize, default_limit=20):
    try:
        number = int(request.GET.get('page', '1'))
        limit = int(request.GET.get('limit', str(default_limit)))
    except ValueError:
        fail(422, 'INVALID_PAGINATION')
    if number < 1 or not 1 <= limit <= 100:
        fail(422, 'INVALID_PAGINATION')
    total = queryset.count()
    return {'items': [serialize(row) for row in queryset[(number - 1) * limit:number * limit]],
            'page': number, 'limit': limit, 'total': total}


def owned(model, user, key, **filters):
    try:
        return model.objects.get(id=parse_uuid(key), user=user, **filters)
    except model.DoesNotExist:
        fail(404, 'NOT_FOUND')


def invalidate_sources(source_field, ids):
    ids = list(ids)
    if not ids:
        return 0
    return AnalysisRun.objects.filter(
        sources__in=AnalysisSource.objects.filter(**{f'{source_field}__in': ids}),
        status='completed',
    ).distinct().update(status='outdated')


def consent(user, scope):
    current = UserConsent.objects.filter(user=user, scope=scope).order_by('-recorded_at', '-id').first()
    return bool(current and current.action == 'grant' and
                (current.expires_at is None or current.expires_at > timezone.now()))


def rate_limit(request, bucket, max_calls, seconds, identity=''):
    remote = request.META.get('REMOTE_ADDR', '')
    key = 'mindcare:rate:' + hashlib.sha256(
        f'{bucket}:{remote}:{identity}'.encode()).hexdigest()
    if cache.add(key, 1, timeout=seconds):
        return
    try:
        if cache.incr(key) > max_calls:
            fail(429, 'RATE_LIMITED')
    except ValueError:
        cache.set(key, 1, timeout=seconds)


def token_for(user):
    issued = int(time.time())
    payload = {'sub': str(user.id), 'role': user.role, 'iat': issued, 'exp': issued + 1800,
               'jti': str(uuid.uuid4()), 'pwd': hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')


def authenticate(request):
    authorization = request.headers.get('Authorization', '')
    if len(authorization) > 8192 or not authorization.startswith('Bearer '):
        fail(401, 'UNAUTHENTICATED')
    try:
        claims = jwt.decode(authorization[7:], settings.SECRET_KEY, algorithms=['HS256'],
                            options={'require': ['sub', 'iat', 'exp', 'jti', 'pwd']})
        now = int(time.time())
        if claims['exp'] - claims['iat'] > 1800 or claims['iat'] > now:
            raise ValueError
        user = User.objects.get(pk=uuid.UUID(claims['sub']))
        if claims.get('pwd') != hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]:
            raise ValueError
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, User.DoesNotExist):
        fail(401, 'UNAUTHENTICATED')
    if user.status != 'active':
        fail(403, 'ACCOUNT_INACTIVE')
    return user


def encrypt(plaintext):
    key = getattr(settings, 'MINDCARE_DATA_ENCRYPTION_KEY', '')
    if not key:
        fail(503, 'ENCRYPTION_UNAVAILABLE')
    from cryptography.fernet import Fernet
    try:
        return Fernet(key).encrypt(plaintext.encode()).decode()
    except (ValueError, TypeError):
        fail(503, 'ENCRYPTION_UNAVAILABLE')


def decrypt(ciphertext):
    key = getattr(settings, 'MINDCARE_DATA_ENCRYPTION_KEY', '')
    if not key:
        fail(503, 'ENCRYPTION_UNAVAILABLE')
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        fail(500, 'DECRYPTION_FAILED')


def signed_invitation(contact):
    fingerprint = hashlib.sha256(contact.contact_value_encrypted.encode()).hexdigest()
    return signing.dumps({'id': str(contact.id), 'fingerprint': fingerprint}, salt='contact-invitation')


def verified_invitation(token):
    from mindcare.models import TrustedContact
    try:
        payload = signing.loads(token, salt='contact-invitation', max_age=86400)
        contact = TrustedContact.objects.get(id=uuid.UUID(payload['id']))
        fingerprint = hashlib.sha256(contact.contact_value_encrypted.encode()).hexdigest()
        if not hmac.compare_digest(payload['fingerprint'], fingerprint):
            raise ValueError
        return contact
    except (signing.BadSignature, ValueError, KeyError, TrustedContact.DoesNotExist):
        fail(400, 'INVALID_OR_EXPIRED_INVITATION')
