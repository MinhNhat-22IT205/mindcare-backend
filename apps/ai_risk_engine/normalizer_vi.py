"""
Vietnamese Anti-Obfuscation, Teencode & No-Diacritics Normalizer (`normalizer_vi.py`).

Handles:
1. Unicode NFC normalization & zero-width / invisible character removal
2. De-obfuscation of symbol-interleaved filter evasion ('t.ự t.ử', 'c_h_ế_t', 't-u t-u', 'n.h.ả.y c.ầ.u')
   using Vietnamese syllable vowel-cluster structure
3. Positive compound shielding ('tự tin', 'tự hào', 'tử tế', 'cố gắng hết sức', ...)
4. Contextual restoration of Vietnamese teencode & unaccented crisis/hyperbole phrases
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad]")

VN_VOWELS = set(
    "aeiouyàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹ"
)

# Positive / benign compounds that share prefixes/syllables with crisis words ('tự', 'tử', 'hết')
# Ordered longest-first; includes both accented and unaccented variants -> canonical accented form
PROTECTED_POSITIVE_COMPOUNDS: list[tuple[str, str]] = [
    ("cố gắng hết sức", "cố gắng hết sức"),
    ("co gang het suc", "cố gắng hết sức"),
    ("nỗ lực hết sức", "nỗ lực hết sức"),
    ("no luc het suc", "nỗ lực hết sức"),
    ("làm hết sức", "làm hết sức"),
    ("lam het suc", "làm hết sức"),
    ("hết sức bình tĩnh", "hết sức bình tĩnh"),
    ("hết sức cẩn thận", "hết sức cẩn thận"),
    ("hết sức mình", "hết sức mình"),
    ("het suc minh", "hết sức mình"),
    ("hết mình", "hết mình"),
    ("het minh", "hết mình"),
    ("hết lòng", "hết lòng"),
    ("het long", "hết lòng"),
    ("tự tin", "tự tin"),
    ("tu tin", "tự tin"),
    ("tự hào", "tự hào"),
    ("tu hao", "tự hào"),
    ("tử tế", "tử tế"),
    ("tu te", "tử tế"),
    ("tự lập", "tự lập"),
    ("tu lap", "tự lập"),
    ("tự học", "tự học"),
    ("tu hoc", "tự học"),
    ("tự chủ", "tự chủ"),
    ("tu chu", "tự chủ"),
    ("tự do", "tự do"),
    ("tu do", "tự do"),
    ("tự nhiên", "tự nhiên"),
    ("tu nhien", "tự nhiên"),
    ("tự giác", "tự giác"),
    ("tu giac", "tự giác"),
    ("tự thưởng", "tự thưởng"),
    ("tu thuong", "tự thưởng"),
    ("tự chăm sóc", "tự chăm sóc"),
    ("tu cham soc", "tự chăm sóc"),
    ("điện tử", "điện tử"),
    ("dien tu", "điện tử"),
    ("phần tử", "phần tử"),
    ("phan tu", "phần tử"),
    ("bất tử", "bất tử"),
    ("bat tu", "bất tử"),
    ("sư tử", "sư tử"),
    ("su tu", "sư tử"),
    ("từ từ", "từ từ"),
]

# Contextual unaccented & teencode phrase restoration (ordered longest-first)
PHRASE_RESTORATION_RULES: list[tuple[str, str]] = [
    # Gen-Z euphemisms & teencode crisis phrases
    ("dang xuat khoi server trai dat", "đăng xuất khỏi server trái đất"),
    ("dang xuat khoi trai dat", "đăng xuất khỏi trái đất"),
    ("dang xuat khoi the gioi", "đăng xuất khỏi thế giới"),
    ("dang xuat khoi server", "đăng xuất khỏi server"),
    ("dang xuat cuoc doi", "đăng xuất cuộc đời"),
    ("muon dang xuat", "muốn đăng xuất"),
    ("mun dang xuat", "muốn đăng xuất"),
    ("ngu mot giac khong bao gio day", "ngủ một giấc không bao giờ dậy"),
    ("ngu 1 giac khong bao gio day", "ngủ một giấc không bao giờ dậy"),
    ("ngu mot giac khong day", "ngủ một giấc không dậy"),
    ("ngu 1 giac k day", "ngủ một giấc không dậy"),
    ("ngu 1 giac ko day", "ngủ một giấc không dậy"),
    ("ngu luon khong day", "ngủ luôn không dậy"),
    ("ngu luon khoi day", "ngủ luôn khỏi dậy"),
    ("ngu luon ko day", "ngủ luôn không dậy"),
    ("ngu luon k day", "ngủ luôn không dậy"),
    ("ngu mai khong day", "ngủ mãi không dậy"),
    ("ngu mai k day", "ngủ mãi không dậy"),
    ("khong muon song nua", "không muốn sống nữa"),
    ("ko muon song nua", "không muốn sống nữa"),
    ("k muon song nua", "không muốn sống nữa"),
    ("k mun song nua", "không muốn sống nữa"),
    ("ko mun song nua", "không muốn sống nữa"),
    ("hong mun song nua", "không muốn sống nữa"),
    ("khum mun song nua", "không muốn sống nữa"),
    ("khong muon song", "không muốn sống"),
    ("ko muon song", "không muốn sống"),
    ("k muon song", "không muốn sống"),
    ("k mun song", "không muốn sống"),
    ("ko mun song", "không muốn sống"),
    ("hong muon song", "không muốn sống"),
    ("khum muon song", "không muốn sống"),
    ("chan song qua", "chán sống quá"),
    ("chan song roi", "chán sống rồi"),
    ("het muon song", "hết muốn sống"),
    ("song lam gi nua", "sống làm gì nữa"),
    ("song chang con y nghia", "sống chẳng còn ý nghĩa"),
    ("viet thu tuyet menh", "viết thư tuyệt mệnh"),
    ("de lai thu tuyet menh", "để lại thư tuyệt mệnh"),
    ("tam biet moi nguoi nhe", "tạm biệt mọi người nhé"),
    ("vinh biet moi nguoi", "vĩnh biệt mọi người"),
    ("tam biet the gioi nay", "tạm biệt thế giới này"),
    ("uong thuoc ngu qua lieu", "uống thuốc ngủ quá liều"),
    ("uong thuoc qua lieu", "uống thuốc quá liều"),
    ("tich tru thuoc ngu", "tích trữ thuốc ngủ"),
    ("mua thuoc ngu de", "mua thuốc ngủ để"),
    ("tu lam dau ban than", "tự làm đau bản thân"),
    ("tu hai ban than", "tự hại bản thân"),
    ("ket lieu ban than", "kết liễu bản thân"),
    ("ket lieu cuoc doi", "kết liễu cuộc đời"),
    ("ket thuc tat ca", "kết thúc tất cả"),
    ("cham het tat ca", "chấm hết tất cả"),
    ("bien mat vinh vien", "biến mất vĩnh viễn"),
    ("bien mat mai mai", "biến mất mãi mãi"),
    ("tan bien mai mai", "tan biến mãi mãi"),
    ("tra het no doi", "trả hết nợ đời"),
    ("nham mat xuoi tay", "nhắm mắt xuôi tay"),
    ("di gap ong ba", "đi gặp ông bà"),
    ("reset cuoc doi", "reset cuộc đời"),
    ("reset kiep nay", "reset kiếp này"),
    ("xoa acc cuoc doi", "xóa acc cuộc đời"),
    ("offline vinh vien", "offline vĩnh viễn"),
    ("met het cuu", "mệt hết cứu"),
    ("het cuu that roi", "hết cứu thật rồi"),
    ("be tac het cuu", "bế tắc hết cứu"),
    ("minh la ganh nang", "mình là gánh nặng"),
    ("chi la ganh nang", "chỉ là gánh nặng"),
    ("khong loi thoat", "không lối thoát"),
    ("ko loi thoat", "không lối thoát"),
    ("k loi thoat", "không lối thoát"),
    # Pragmatic hyperbole / recovery / quote phrases (for accurate Layer 2 disambiguation)
    ("deadline di muon chet", "deadline dí muốn chết"),
    ("deadline di mun chet", "deadline dí muốn chết"),
    ("code loi muon nhay lau", "code lỗi muốn nhảy lầu"),
    ("fix bug muon nhay lau", "fix bug muốn nhảy lầu"),
    ("rot mon chac xiu", "rớt môn chắc xỉu"),
    ("rot mon chac chet", "rớt môn chắc chết"),
    ("nong chet mat", "nóng chết mất"),
    ("doi muon xiu", "đói muốn xỉu"),
    ("met muon xiu", "mệt muốn xỉu"),
    ("loi bai hat", "lời bài hát"),
    ("trong bai hat", "trong bài hát"),
    ("trong phim", "trong phim"),
    ("nam ngoai tung nghi quan", "năm ngoái từng nghĩ quẩn"),
    ("nhung gio on roi", "nhưng giờ ổn rồi"),
    ("gio yeu doi hon roi", "giờ yêu đời hơn rồi"),
    # Prompt injection / bypass unaccented
    ("bo qua huong dan truoc", "bỏ qua hướng dẫn trước"),
    ("bo qua moi quy tac", "bỏ qua mọi quy tắc"),
    ("dong vai nha van trinh tham", "đóng vai nhà văn trinh thám"),
    ("cach tu tu khong dau", "cách tự tử không đau"),
    ("lieu luong gay tu vong", "liều lượng gây tử vong"),
    # Short high-risk two-word phrases (evaluated AFTER protected positive compounds!)
    ("muon tu tu", "muốn tự tử"),
    ("mun tu tu", "muốn tự tử"),
    ("dinh tu tu", "định tự tử"),
    ("muon tu sat", "muốn tự sát"),
    ("mun tu sat", "muốn tự sát"),
    ("chi muon chet", "chỉ muốn chết"),
    ("tha chet di", "thà chết đi"),
    ("muon chet", "muốn chết"),
    ("mun chet", "muốn chết"),
    ("nhay cau", "nhảy cầu"),
    ("nhay lau", "nhảy lầu"),
    ("cat co tay", "cắt cổ tay"),
    ("rach co tay", "rạch cổ tay"),
    ("rach tay", "rạch tay"),
    ("treo co", "treo cổ"),
    ("day thung", "dây thừng"),
    ("tu sat", "tự sát"),
    ("tu tu", "tự tử"),
]

# Safe token-level teencode map for common chat tokens
TEENCODE_TOKEN_MAP: dict[str, str] = {
    "k": "không",
    "ko": "không",
    "kh": "không",
    "hok": "không",
    "hong": "không",
    "hông": "không",
    "khum": "không",
    "mun": "muốn",
    "mún": "muốn",
    "dc": "được",
    "đc": "được",
    "dk": "được",
    "mk": "mình",
    "mik": "mình",
    "lun": "luôn",
    "wa": "quá",
    "vs": "với",
    "rùi": "rồi",
    "bik": "biết",
    "bit": "biết",
    "j": "gì",
    "cx": "cũng",
    "bh": "bây giờ",
    "ntn": "như thế nào",
    "ngta": "người ta",
}

# Leetspeak obfuscation inside targeted crisis words
LEET_OBFUSCATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!\w)ch[3eêế]t(?!\w)"), "chết"),
    (re.compile(r"(?<!\w)t[uưự][\s.\-_*]*t[uưử](?!\w)"), "tự tử"),
    (re.compile(r"(?<!\w)t[uưự][\s.\-_*]*s[4aá]t(?!\w)"), "tự sát"),
    (re.compile(r"(?<!\w)nh[4aả]y[\s.\-_*]*c[4aầ]u(?!\w)"), "nhảy cầu"),
    (re.compile(r"(?<!\w)nh[4aả]y[\s.\-_*]*l[4aầ]u(?!\w)"), "nhảy lầu"),
    (re.compile(r"(?<!\w)r[4aạ]ch[\s.\-_*]*t[4a]y(?!\w)"), "rạch tay"),
]

# Matches tokens with interleaved punctuation like t.ự, c_h_ế_t, n.h.ả.y, t-u, tự_tử, ch.ết
INTERLEAVED_PUNCT_RE = re.compile(
    r"(?<!\w)([^\W\d_]+(?:[.\-_*~|/\\]+[^\W\d_]+)+)(?!\w)",
    re.UNICODE,
)

# Matches single-space-interleaved single letters of length >= 4 letters (e.g. 'c h ế t', 't ự t ử')
SPACED_SINGLE_LETTERS_RE = re.compile(
    r"(?<!\w)(?:[^\W\d_]\s){3,}[^\W\d_](?!\w)",
    re.UNICODE,
)


def _has_vn_vowel(segment: str) -> bool:
    return any(ch in VN_VOWELS for ch in segment.lower())


def _deobfuscate_interleaved_match(match: re.Match[str]) -> tuple[str, bool]:
    token = match.group(1)
    parts = [p for p in re.split(r"[.\-_*~|/\\]+", token) if p]
    if len(parts) < 2:
        return token, False

    # Case 1: All fragments are single characters (e.g. t.ự -> tự, c_h_ế_t -> chết, n.h.ả.y -> nhảy, t-u -> tu)
    if all(len(p) == 1 for p in parts):
        return "".join(parts), True

    # Case 2: Multi-character fragments: if at least one fragment has NO vowel (onset/coda split like ch.ết or nh.ảy),
    # concatenate into one syllable; if every fragment has its own vowel (tự_tử, nhảy-lầu), join with space!
    if all(_has_vn_vowel(p) for p in parts):
        return " ".join(parts), True
    return "".join(parts), True


def strip_vietnamese_diacritics(text: str) -> str:
    if not text:
        return ""
    replaced = text.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize("NFD", replaced)
    without_marks = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", without_marks)


@dataclass
class NormalizedTextResult:
    raw_text: str
    normalized_text: str
    folded_ascii: str
    obfuscation_detected: bool = False
    protected_spans: list[str] = field(default_factory=list)


def normalize_vietnamese(text: str) -> NormalizedTextResult:
    """
    Full deterministic normalization pipeline (< 1.5 ms on typical chat messages).
    """
    raw = text or ""
    # 1. NFC + remove invisible zero-width characters
    cleaned = unicodedata.normalize("NFC", raw)
    had_zw = bool(ZERO_WIDTH_RE.search(cleaned))
    if had_zw:
        cleaned = ZERO_WIDTH_RE.sub("", cleaned)

    lowered = cleaned.lower()
    obfuscated = had_zw

    # 2. Collapse spaced-out single-character evasions like 'c h ế t'
    def _collapse_spaced(m: re.Match[str]) -> str:
        nonlocal obfuscated
        obfuscated = True
        letters = m.group(0).split()
        # Heuristic: 4 letters of 2+2 syllables like 't ự t ử' -> 'tự tử' if both halves have vowels
        if len(letters) == 4 and _has_vn_vowel("".join(letters[:2])) and _has_vn_vowel("".join(letters[2:])) and letters[2] not in VN_VOWELS:
            return "".join(letters[:2]) + " " + "".join(letters[2:])
        return "".join(letters)

    lowered = SPACED_SINGLE_LETTERS_RE.sub(_collapse_spaced, lowered)

    # 3. Collapse punctuation-interleaved tokens ('t.ự t.ử', 'c_h_ế_t', 't-u t-u', 'n.h.ả.y c.ầ.u')
    def _replace_interleaved(m: re.Match[str]) -> str:
        nonlocal obfuscated
        rep, changed = _deobfuscate_interleaved_match(m)
        if changed:
            obfuscated = True
        return rep

    lowered = INTERLEAVED_PUNCT_RE.sub(_replace_interleaved, lowered)

    # Normalize whitespace and strip decorative punctuation borders while keeping words clean
    lowered = re.sub(r"[^\w\sàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()

    # 4. Shield positive / benign compound words BEFORE unaccented crisis expansion
    protected_found: list[str] = []
    placeholders: dict[str, str] = {}
    for idx, (variant, canonical) in enumerate(PROTECTED_POSITIVE_COMPOUNDS):
        pat = re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)")
        if pat.search(lowered):
            ph = f"__mcprot{idx}__"
            placeholders[ph] = canonical
            protected_found.append(canonical)
            lowered = pat.sub(ph, lowered)

    # 5. Multi-word teencode & unaccented phrase restoration (run BEFORE single-word leetspeak)
    for src_phrase, dst_phrase in PHRASE_RESTORATION_RULES:
        if src_phrase in lowered:
            lowered = re.sub(rf"(?<!\w){re.escape(src_phrase)}(?!\w)", dst_phrase, lowered)

    # 6. Leetspeak / mixed-symbol crisis restoration
    for pat, replacement in LEET_OBFUSCATION_PATTERNS:
        if pat.search(lowered):
            obfuscated = True
            lowered = pat.sub(replacement, lowered)

    # 7. Token-level teencode normalization (skipping placeholders)
    tokens = lowered.split()
    norm_tokens: list[str] = []
    for i, tok in enumerate(tokens):
        if tok.startswith("__mcprot"):
            norm_tokens.append(tok)
            continue
        if tok == "nua" and i > 0 and norm_tokens[-1] in ("sống", "chịu", "ở", "dậy", "thở"):
            norm_tokens.append("nữa")
        elif tok in TEENCODE_TOKEN_MAP:
            norm_tokens.append(TEENCODE_TOKEN_MAP[tok])
        else:
            norm_tokens.append(tok)
    lowered = " ".join(norm_tokens)

    # Re-run key restored phrases after token-level & leetspeak expansion
    post_token_fixes = [
        ("không muốn song nữa", "không muốn sống nữa"),
        ("không muốn song", "không muốn sống"),
        ("muốn chet", "muốn chết"),
        ("muon chết", "muốn chết"),
        ("mun chết", "muốn chết"),
        ("chi muốn chết", "chỉ muốn chết"),
        ("muon tự tử", "muốn tự tử"),
        ("mun tự tử", "muốn tự tử"),
        ("dinh tự tử", "định tự tử"),
        ("muon tự sát", "muốn tự sát"),
        ("muon nhảy cầu", "muốn nhảy cầu"),
        ("muon nhảy lầu", "muốn nhảy lầu"),
        ("ngủ luôn không day", "ngủ luôn không dậy"),
        ("ngủ 1 giac không day", "ngủ một giấc không dậy"),
    ]
    for src_p, dst_p in post_token_fixes:
        if src_p in lowered:
            lowered = re.sub(rf"(?<!\w){re.escape(src_p)}(?!\w)", dst_p, lowered)

    # 8. Restore shielded positive compounds
    for ph, canonical in placeholders.items():
        lowered = lowered.replace(ph, canonical)

    lowered = re.sub(r"\s+", " ", lowered).strip()
    folded = strip_vietnamese_diacritics(lowered)

    return NormalizedTextResult(
        raw_text=raw,
        normalized_text=lowered,
        folded_ascii=folded,
        obfuscation_detected=obfuscated,
        protected_spans=protected_found,
    )
