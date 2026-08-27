import re
import difflib
import unicodedata

# ---------------------------------------------------------------------------
# Arabic & International Team Name Normalization and Matching Module
# Handles dialectal transliteration discrepancies, affix stripping,
# phonetic equivalence, and canonical cross-matching.
# ---------------------------------------------------------------------------

# Common prefixes and noise words found in Arabic club names
_ARABIC_PREFIXES = [
    r'^نادي\s+',
    r'^فريق\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:أف|اف|ف)\s+(?:سي|س)\s+',
    r'^(?:إف|اف|ف)\s+(?:سي|س)\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:إس|اس|س)\s+',
    r'^(?:إس|اس|س)\s+(?:سي|س)\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:سي|س)\s+',
    r'^(?:سي|س)\s+(?:دي|د)\s+',
    r'^(?:سي|س)\s+(?:إف|اف|ف)\s+',
    r'^(?:fc|afc|cf|sc|ac|cd|sd|as|sk|fk|bsc|tsg|vfb|vfl|sv|ssv|fsv|rb|rsc|rc|cs|us|ogc)\s+',
]

# Common suffixes found in Arabic club names
_ARABIC_SUFFIXES = [
    r'\s+(?:إف|اف|ف)\s+(?:سي|س)$',
    r'\s+(?:إس|اس|س)\s+(?:سي|س)$',
    r'\s+(?:تي|ت)\s+(?:سي|س)$',
    r'\s+(?:fc|afc|cf|sc|ac|cd|sd|tc|athletic|city|united|town|rovers|wanderers|albion)$',
]

# Shared generic club prefixes that shouldn't be the sole basis for a match
_GENERIC_CLUB_WORDS = {
    "ريال", "انتر", "إنتر", "مانشستر", "بايرن", "باير", "اتلتيكو", "أتلتيكو", "اتلتيك", "أتلتيك",
    "سبورتينغ", "سبورتنج", "دينامو", "النجم", "شباب", "اتحاد", "الاتحاد", "اهلي", "الاهلي",
    "نادي", "فريق", "اولمبيك", "أولمبيك", "لوكوموتيف", "سسكا", "سبارتاك", "ريد بول", "رد بول"
}


def strip_arabic_diacritics_and_noise(text: str) -> str:
    """Strips tashkeel (diacritics), tatweel/kashida, and zero-width characters."""
    if not text:
        return ""
    # Strip tashkeel / harakat and tatweel
    t = re.sub(r'[\u064B-\u065F\u0670\u0640]', '', text)
    # Strip zero-width and invisible unicode characters
    t = re.sub(r'[\u200B-\u200F\u202A-\u202E\uFEFF]', '', t)
    return t


def strip_club_affixes(text: str) -> str:
    """Removes non-distinguishing organizational prefixes and suffixes from club names."""
    if not text:
        return ""
    t = text.strip()
    # Strip prefixes iteratively
    changed = True
    while changed:
        before = t
        for p in _ARABIC_PREFIXES:
            t = re.sub(p, '', t, flags=re.IGNORECASE).strip()
        changed = (t != before)

    # Strip suffixes iteratively
    changed = True
    while changed:
        before = t
        for s in _ARABIC_SUFFIXES:
            t = re.sub(s, '', t, flags=re.IGNORECASE).strip()
        changed = (t != before)

    return t.strip()


def normalize_arabic_text(text: str, aggressive: bool = False) -> str:
    """
    Standardizes Arabic strings by normalizing character shapes, stripping noise,
    and optionally applying aggressive phonetic normalization for transliterated club names.
    """
    if not text:
        return ""

    t = strip_arabic_diacritics_and_noise(text.strip())

    # Normalize alef variants (إ, أ, آ, ٱ -> ا)
    t = re.sub(r'[إأآٱ]', 'ا', t)

    # Normalize teh marbuta (ة -> ه)
    t = re.sub(r'ة', 'ه', t)

    # Normalize alif maksura and yaa forms (ى -> ي)
    t = re.sub(r'[ىي]', 'ي', t)

    # Normalize hamza on waw and yaa
    t = re.sub(r'ؤ', 'و', t)
    t = re.sub(r'ئ', 'ي', t)

    # Lowercase any Latin characters
    t = t.lower()

    # Strip club noise prefixes/suffixes
    t = strip_club_affixes(t)

    # Collapse repeated whitespace and punctuation
    t = re.sub(r'[-_/|]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()

    if aggressive:
        # 1. Foreign transliteration consonant equivalence (Egyptian/Levantine 'ج' vs Gulf/Maghrebi 'غ' / 'ق')
        # Examples: سيلتا فيغو <-> سيلتا فيجو, فرانكفورت <-> فرانكفورد
        t = re.sub(r'[غق]', 'ج', t)

        # 2. Transliteration sound shifts: 'تس' -> 'س', 'تش' -> 'ش'
        t = re.sub(r'تس', 'س', t)
        t = re.sub(r'تش', 'ش', t)

        # 3. Soften optional internal vowels often dropped in transliteration (e.g. بيلباو -> بلباو)
        t = re.sub(r'(?<=[\u0600-\u06FF])ي(?=[\u0600-\u06FF]{2,})', '', t)

        # 4. Trailing Arabic nisba adjective 'ي' (e.g. فيرينتسفاروشي -> فيرينتسفاروش, إلتشي -> إلتش)
        if len(t) > 5 and t.endswith('ي'):
            t = t[:-1].strip()

    return t


def get_arabic_match_fingerprint(text: str) -> str:
    """Returns a canonical normalized phonetic fingerprint for Arabic team comparison."""
    return normalize_arabic_text(text, aggressive=True)


def are_arabic_names_equivalent(name1: str, name2: str) -> bool:
    """
    Determines if two Arabic team name strings represent the same team,
    accounting for dialect differences, affixes, and transliteration variations.
    """
    if not name1 or not name2:
        return False

    n1_clean = name1.strip()
    n2_clean = name2.strip()

    # Level 1: Exact match
    if n1_clean.lower() == n2_clean.lower():
        return True

    # Level 2: Standard orthographic normalization match
    norm1 = normalize_arabic_text(n1_clean, aggressive=False)
    norm2 = normalize_arabic_text(n2_clean, aggressive=False)
    if norm1 and norm2 and norm1 == norm2:
        return True

    # Level 3: Aggressive phonetic transliteration fingerprint match
    fp1 = normalize_arabic_text(n1_clean, aggressive=True)
    fp2 = normalize_arabic_text(n2_clean, aggressive=True)
    if fp1 and fp2 and fp1 == fp2:
        return True

    # Level 4: Substring / token containment (e.g. 'ويمبلدون' inside 'إيه أف سي ويمبلدون')
    if fp1 and fp2:
        words1 = [w for w in fp1.split() if w not in _GENERIC_CLUB_WORDS]
        words2 = [w for w in fp2.split() if w not in _GENERIC_CLUB_WORDS]
        if words1 and words2 and set(words1) == set(words2):
            return True

        if (len(fp1) >= 4 and fp1 in fp2) or (len(fp2) >= 4 and fp2 in fp1):
            # Guard against false positives on generic prefixes (e.g. 'ريال' matching 'ريال مدريد')
            shorter = fp1 if len(fp1) <= len(fp2) else fp2
            if shorter not in _GENERIC_CLUB_WORDS:
                return True

    # Level 5: High similarity ratio on aggressive fingerprints (> 0.85) with generic word guard
    if fp1 and fp2:
        words1 = fp1.split()
        words2 = fp2.split()
        # If both have multi-word names starting with a generic word (e.g. Real, Inter, Manchester),
        # verify the specific distinguishing words also match
        if len(words1) > 1 and len(words2) > 1 and words1[0] in _GENERIC_CLUB_WORDS and words2[0] in _GENERIC_CLUB_WORDS:
            specific1 = " ".join(words1[1:])
            specific2 = " ".join(words2[1:])
            if difflib.SequenceMatcher(None, specific1, specific2).ratio() >= 0.82:
                return True
            return False

        ratio = difflib.SequenceMatcher(None, fp1, fp2).ratio()
        if ratio >= 0.85:
            return True

    return False


def _strip_accents(text: str) -> str:
    """Removes accents and diacritics from Latin characters (e.g. Ferencváros -> Ferencvaros)."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def normalize_english_team(name: str) -> str:
    """
    Standardizes English team names by stripping accents, punctuation, and common club suffixes.
    Example: 'Ferencvárosi TC' -> 'ferencvaros', 'Chelsea FC' -> 'chelsea'.
    """
    if not name:
        return ""
    t = _strip_accents(name).lower().strip()

    # Strip noise club abbreviations
    noise_patterns = [
        r'\b(?:fc|cf|afc|sc|ac|cd|sd|tc|bsc|tsg|vfb|vfl|sv|ssv|fsv|rb|rsc|rc|cs|us|ogc)\b',
        r'\b(?:club|football\s+club|de\s+fútbol|de\s+futbol|calcio)\b',
    ]
    for np in noise_patterns:
        t = re.sub(np, '', t).strip()

    # Strip trailing suffixes like 'i' in Hungarian (Ferencvárosi -> ferencvaros)
    t = re.sub(r'(\w{4,})i\b', r'\1', t)

    # Clean non-alphanumeric
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def are_english_teams_equivalent(name1: str, name2: str) -> bool:
    """Checks if two English team names refer to the same club (e.g. Ferencvárosi TC vs Ferencváros)."""
    if not name1 or not name2:
        return False
    if name1.strip().lower() == name2.strip().lower():
        return True

    norm1 = normalize_english_team(name1)
    norm2 = normalize_english_team(name2)
    if norm1 and norm2 and norm1 == norm2:
        return True

    tokens1 = set(norm1.split())
    tokens2 = set(norm2.split())
    if tokens1 and tokens2 and (tokens1.issubset(tokens2) or tokens2.issubset(tokens1)):
        # Ensure at least one substantial token (>3 chars) matches
        common = tokens1 & tokens2
        if any(len(tok) >= 4 for tok in common):
            return True

    return False
