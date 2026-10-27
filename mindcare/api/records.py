from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from mindcare.api.common import (api, body, date_range, decrypt, encrypt,
                                 fail, invalidate_sources, local_today, owned, page,
                                 parse_datetime, parse_date, respond, text_value)
from mindcare.models import (AnalysisReport, AnalysisSource, CheckIn,
                             JournalEntry)


SCORES = ('stress_level', 'mood_level', 'anxiety_level', 'energy_level')
CHECKIN_FIELDS = {*SCORES, 'sleep_hours', 'note'}


def checkin_data(item):
    return {'id': str(item.id), 'checkin_date': item.checkin_date, 'stress_level': item.stress_level,
            'mood_level': item.mood_level, 'anxiety_level': item.anxiety_level,
            'energy_level': item.energy_level, 'sleep_hours': item.sleep_hours,
            'note': item.note, 'recorded_at': item.recorded_at, 'updated_at': item.updated_at}


def checkin_fields(data):
    result = {}
    for field, value in data.items():
        if field in SCORES:
            if value is not None and (type(value) is not int or not 0 <= value <= 10):
                fail(422, 'INVALID_CHECKIN_SCORE')
        elif field == 'sleep_hours':
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 24):
                fail(422, 'INVALID_CHECKIN_SCORE')
        elif field == 'note':
            value = text_value(value, field, 2000, blank=True)
        result[field] = value
    return result


def nonempty_checkin(data):
    if not any(data.get(field) is not None for field in (*SCORES, 'sleep_hours')) and not data.get('note'):
        fail(422, 'EMPTY_CHECKIN')


@api({'GET', 'POST'}, {'user'})
def checkins(request):
    user = request.api_user
    if request.method == 'GET':
        start, end = date_range(request, user, default_days=30, max_days=366)
        queryset = CheckIn.objects.filter(user=user, checkin_date__range=(start, end)).order_by('-checkin_date', '-id')
        return respond(request, page(request, queryset, checkin_data, 30))
    data = body(request, CHECKIN_FIELDS | {'checkin_date'})
    day = parse_date(data['checkin_date']) if 'checkin_date' in data else local_today(user)
    if day > local_today(user):
        fail(422, 'INVALID_DATE_RANGE')
    fields = checkin_fields({key: value for key, value in data.items() if key in CHECKIN_FIELDS})
    nonempty_checkin(fields)
    try:
        with transaction.atomic():
            item = CheckIn.objects.create(user=user, checkin_date=day, recorded_at=timezone.now(), **fields)
    except IntegrityError:
        fail(409, 'DUPLICATE_CHECKIN')
    return respond(request, {**checkin_data(item), 'light_analysis_queued': False}, 201)


@api({'GET', 'PATCH'}, {'user'})
def checkin_detail(request, checkin_id):
    item = owned(CheckIn, request.api_user, checkin_id)
    if request.method == 'GET':
        return respond(request, checkin_data(item))
    data = body(request, CHECKIN_FIELDS)
    if not data:
        fail(422, 'VALIDATION_ERROR')
    fields = checkin_fields(data)
    for key, value in fields.items():
        setattr(item, key, value)
    nonempty_checkin({field: getattr(item, field) for field in CHECKIN_FIELDS})
    with transaction.atomic():
        item.save(update_fields=[*fields, 'updated_at'])
        count = invalidate_sources('check_in', [item.id])
    return respond(request, {**checkin_data(item), 'affected_reports_marked_outdated': count})


def journal_data(item):
    return {'id': str(item.id), 'content': decrypt(item.content),
            'experienced_at': item.experienced_at, 'created_at': item.created_at,
            'updated_at': item.updated_at}


@api({'GET', 'POST'}, {'user'})
def journals(request):
    user = request.api_user
    if request.method == 'GET':
        start, end = date_range(request, user, default_days=30, max_days=366)
        queryset = JournalEntry.objects.filter(user=user, experienced_at__date__range=(start, end)).order_by('-created_at', '-id')
        return respond(request, page(request, queryset, lambda item: {
            'id': str(item.id), 'experienced_at': item.experienced_at,
            'preview': decrypt(item.content)[:120], 'created_at': item.created_at}, 20))
    data = body(request, {'content', 'experienced_at'}, {'content'})
    content = text_value(data['content'], 'content', 10000)
    experienced_at = parse_datetime(data['experienced_at']) if data.get('experienced_at') else timezone.now()
    if experienced_at > timezone.now() + timedelta(minutes=5):
        fail(422, 'VALIDATION_ERROR')
    item = JournalEntry.objects.create(user=user, content=encrypt(content), experienced_at=experienced_at)
    return respond(request, {'id': str(item.id), 'experienced_at': item.experienced_at,
                             'created_at': item.created_at, 'light_analysis_queued': False}, 201)


@api({'GET', 'PATCH', 'DELETE'}, {'user'})
def journal_detail(request, journal_id):
    item = owned(JournalEntry, request.api_user, journal_id)
    if request.method == 'GET':
        return respond(request, journal_data(item))
    if request.method == 'DELETE':
        with transaction.atomic():
            invalidate_sources('journal', [item.id])
            AnalysisReport.objects.filter(analysis_run__sources__journal=item).delete()
            AnalysisSource.objects.filter(journal=item).delete()
            item.delete()
        return respond(request, status=204)
    data = body(request, {'content', 'experienced_at'})
    if not data:
        fail(422, 'VALIDATION_ERROR')
    if 'content' in data:
        item.content = encrypt(text_value(data['content'], 'content', 10000))
    if 'experienced_at' in data:
        item.experienced_at = parse_datetime(data['experienced_at'])
    with transaction.atomic():
        item.save(update_fields=[*data, 'updated_at'])
        count = invalidate_sources('journal', [item.id])
    return respond(request, {'id': str(item.id), 'updated_at': item.updated_at,
                             'affected_reports_marked_outdated': count})


@api({'GET'}, {'user'})
def journal_prompts(request):
    prompts = {'stress': [('stress-01', 'Điều gì khiến bạn áp lực hôm nay?')],
               'gratitude': [('gratitude-01', 'Hôm nay bạn biết ơn điều gì?')],
               'reflection': [('reflection-01', 'Điều gì khiến bạn mệt nhất hôm nay?')]}
    category = request.GET.get('category')
    if category and category not in prompts:
        fail(422, 'INVALID_CATEGORY')
    items = [{'id': key, 'text': prompt} for name, values in prompts.items()
             if not category or category == name for key, prompt in values]
    return respond(request, {'items': items})
