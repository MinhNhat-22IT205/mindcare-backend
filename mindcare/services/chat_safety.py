"""Local screening rules; no inference calls or external side effects.

These rules detect explicit signals and block known unsafe output patterns.
An absence of a match is never a clinical safety assessment.
"""
import re
import unicodedata


FALLBACK_RESPONSE = (
    'Mình chưa thể tạo phản hồi phù hợp lúc này. Bạn có thể thử lại sau; '
    'nếu cần hỗ trợ ngay, hãy liên hệ người bạn tin tưởng hoặc dịch vụ khẩn cấp tại nơi bạn sống.'
)
SAFETY_RESPONSE = (
    'Mình rất lo khi nghe điều bạn chia sẻ. Nếu bạn đang gặp nguy hiểm ngay lúc này, '
    'hãy liên hệ dịch vụ khẩn cấp tại nơi bạn sống hoặc nhờ một người bạn tin tưởng '
    'ở bên bạn. Tín hiệu cần hỗ trợ trong ứng dụng đang chờ xem xét.'
)


def normalized(text):
    text = unicodedata.normalize('NFKD', text.casefold()).replace('đ', 'd')
    return re.sub(r'\s+', ' ', ''.join(c for c in text if not unicodedata.combining(c)))


def detect_safety(text):
    value = normalized(text)
    # Remove explicit denials without erasing a later, affirmative sentence.
    value = re.sub(r'\b(?:toi|minh|em|i) (?:khong muon|khong dinh|do not want to|don.t want to) '
                   r'(?:tu tu|tu hai|chet|kill myself|hurt myself)\b', '', value)
    personal = re.search(
        r'\b(?:toi|minh|em|tao)\b[^.!?\n]{0,70}\b(?:muon tu tu|dinh tu tu|se tu tu|'
        r'muon chet|khong muon song nua|muon tu hai|muon tu lam hai|muon giet|dinh giet)\b|'
        r'\bi (?:want to|will|plan to|am going to) (?:kill myself|hurt myself|end my life|kill someone)\b|'
        r"\bi (?:do not|don't|dont) want to live (?:anymore|any more)\b|"
        r'\b(?:toi|minh|em) (?:vua|da) (?:uong thuoc qua lieu|tu cat|tu hai)\b', value)
    human_help = re.search(r'\b(?:toi|minh|em) can (?:ho tro khan cap|nguoi ho tro khung hoang)\b', value)
    if not personal and not human_help:
        return None
    urgent = re.search(r'\b(?:ngay bay gio|ngay luc nay|toi nay|right now|tonight|'
                       r'vua uong thuoc qua lieu|da uong thuoc qua lieu|vua tu cat)\b', value)
    return {'severity': 'urgent' if urgent else 'needs_review',
            'reason': 'Tin nhắn có tín hiệu cần người hỗ trợ xem xét.'}


def guard_output(text):
    value = normalized(text)
    blocked = (
        r'\b(?:ban|you) (?:chac chan |definitely )?(?:mac (?:benh|chung)|bi tram cam|have depression|have bipolar)\b',
        r'\b(?:toi|minh|i) (?:da |have |already )?(?:goi nguoi than|gui canh bao|'
        r'lien he (?:nguoi than|cap cuu)|contacted|called your|sent an alert)\b',
        r'\b(?:canh bao|thong bao|alert) (?:cua ban )?(?:da duoc gui|da gui|was sent|has been sent)\b',
        r'\b(?:ban|you) (?:hoan toan an toan|are completely safe|are guaranteed safe)\b',
        r'\b(?:uong|dung|take)\b.{0,45}\b\d+(?:[.,]\d+)?\s*mg\b',
        r'\b(?:hay|nen|you should) (?:tu tu|tu hai|uong thuoc qua lieu|kill yourself|hurt yourself)\b',
        r'\b(?:toi|minh) (?:cung )?(?:da )?tung (?:bi tram cam|chia tay|mat nguoi than|that nghiep)\b',
        r'\b(?:goi|hotline|call|lien he)\s*:?\s*\+?\d{3,}\b',
    )
    return not any(re.search(pattern, value) for pattern in blocked)


def asks_question(text):
    value = normalized(text)
    return ('?' in text or '？' in text or bool(re.search(
        r'\b(?:ban co (?:muon|the)|ban muon .{0,60}khong|ban .{0,40}(?:the nao|dieu gi)|'
        r'can you|could you|would you|do you|what (?:do|would|is|are)|how (?:do|are))\b', value)))
