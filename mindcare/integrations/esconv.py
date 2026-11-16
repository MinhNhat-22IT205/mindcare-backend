"""One Gradio submission followed by its SSE result; never retry inference.

Protocol: https://www.gradio.app/guides/querying-gradio-apps-with-curl
"""
import json
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from mindcare.chat_contract import SPACE_ERROR_CODES, validate_wire_payload


class ProviderUnavailable(RuntimeError):
    def __init__(self, message, code='MODEL_UNAVAILABLE'):
        super().__init__(message)
        self.code = code if code in SPACE_ERROR_CODES else 'MODEL_UNAVAILABLE'


def space_error_code(data):
    """Extract only known diagnostic codes; never relay a raw Gradio error."""
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return 'MODEL_UNAVAILABLE'
    if isinstance(value, dict):
        value = value.get('error')
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, str) and value.startswith('ESCONV_'):
        code = value[len('ESCONV_'):]
        if code in SPACE_ERROR_CODES:
            return code
    return 'MODEL_UNAVAILABLE'


def respond(payload):
    validate_wire_payload(payload)
    root = settings.MINDCARE_ESCONV_SPACE_URL.rstrip('/')
    parts = urlsplit(root)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or
            parts.query or parts.fragment):
        raise ProviderUnavailable('Configure an HTTPS Gradio Space origin.')
    api_name = settings.MINDCARE_ESCONV_API_NAME
    if not re.fullmatch(r'[a-zA-Z0-9_]+', api_name):
        raise ProviderUnavailable('Invalid Gradio API name.')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'User-Agent': 'MindCare/2.1 (ESConv)'}
    if settings.MINDCARE_ESCONV_HF_TOKEN:
        headers['Authorization'] = f'Bearer {settings.MINDCARE_ESCONV_HF_TOKEN}'
    deadline = time.monotonic() + settings.MINDCARE_ESCONV_TIMEOUT_SECONDS
    endpoint = f'{root}/gradio_api/call/{api_name}'
    wire = json.dumps({'data': [json.dumps(payload, ensure_ascii=False)]}, ensure_ascii=False).encode()

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('ESConv request timed out.')
        return seconds

    with urlopen(Request(endpoint, data=wire, headers=headers, method='POST'),
                 timeout=remaining()) as response:
        submitted = json.loads(response.read(4097))
    event_id = submitted.get('event_id') if isinstance(submitted, dict) else None
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event_id):
        raise ProviderUnavailable('Invalid Gradio event ID.')
    stream_headers = {**headers, 'Accept': 'text/event-stream'}
    with urlopen(Request(f'{endpoint}/{event_id}', headers=stream_headers),
                 timeout=remaining()) as response:
        event, data, total = '', [], 0
        while True:
            timeout = remaining()
            # urllib's socket timeout is per read. Refresh it so a late
            # heartbeat cannot start another full request timeout period.
            try:
                response.fp.raw._sock.settimeout(timeout)
            except AttributeError:
                pass  # In-memory transports used by tests have no socket.
            # Heartbeats do not extend the overall request deadline.
            # Limit both individual lines and the whole event stream.
            line = response.readline(65537)
            total += len(line)
            if len(line) > 65536 or total > 262144:
                raise ProviderUnavailable('Gradio event stream exceeds the limit.')
            if not line:
                raise ProviderUnavailable('Gradio stream ended without a result.')
            line = line.decode('utf-8').rstrip('\r\n')
            if line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                data.append(line[5:].lstrip())
            elif not line:
                if event == 'error':
                    raise ProviderUnavailable('Gradio inference failed.', code=space_error_code('\n'.join(data)))
                if event == 'complete':
                    result = json.loads('\n'.join(data))
                    if not isinstance(result, list) or len(result) != 1:
                        raise ProviderUnavailable('Expected one Gradio output.')
                    return result[0]
                event, data = '', []
