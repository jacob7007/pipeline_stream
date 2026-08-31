import json
from pathlib import Path
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
    r'^(?:النادي|نادي)\s+',
    r'^(?:الفريق|فريق)\s+',
    r'^(?:الجمعية|جمعية)\s+',
    r'^(?:ستاد|استاد)\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:أف|اف|ف)\s+(?:سي|س)\s+',
    r'^(?:إف|اف|ف)\s+(?:سي|س)\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:إس|اس|س)\s+',
    r'^(?:إس|اس|س)\s+(?:سي|س)\s+',
    r'^(?:إيه|ايه|اي|أيه)\s+(?:سي|س)\s+',
    r'^(?:سي|س)\s+(?:دي|د)\s+',
    r'^(?:سي|س)\s+(?:إف|اف|ف)\s+',
    r'^(?:fc|afc|cf|sc|ac|cd|sd|as|sk|fk|bsc|tsg|vfb|vfl|sv|ssv|fsv|rb|rsc|rc|cs|us|ogc)\s+',
]

# Common suffixes and internal noise words found in Arabic club names
_ARABIC_SUFFIXES = [
    r'\s+(?:إف|اف|ف)\s+(?:سي|س)$',
    r'\s+(?:إس|اس|س)\s+(?:سي|س)$',
    r'\s+(?:تي|ت)\s+(?:سي|س)$',
    r'\s+(?:الرياضي|الرياضية|رياضي|البيضاوي|بيضاوي|القاهري|قاهري|البورسعيدي|بورسعيدي|الرباطي|رباطي|الجزائر|الجزاير|العاصمة|العاصمه|العراقي|التونسي|المصري|مصري|السعودي|سعودي|القطري|قطري|تيزي\s+وزو|للألعاب\s+الرياضية|للالعاب\s+الرياضيه)$',
    r'\s+(?:fc|afc|cf|sc|ac|cd|sd|tc|athletic|city|united|town|rovers|wanderers|albion)$',
]

# Generic noise words in Arabic that shouldn't be the sole basis for a match
_GENERIC_CLUB_WORDS = {
    "نادي", "فريق", "جمعية", "اولمبيك", "أولمبيك", "لوكوموتيف", "سسكا", "سبارتاك", "ريد بول", "رد بول", "ستاد", "استاد"
}

# Arabic country / regional qualifiers that differentiate clubs sharing a common name
_ARABIC_CONFLICTING_PAIRS: set[frozenset] = {
    frozenset({"السعودي", "السوداني"}),
    frozenset({"السعودي", "المصري"}),
    frozenset({"السعودي", "الليبي"}),
    frozenset({"السعودي", "القطري"}),
    frozenset({"السعودي", "الكويتي"}),
    frozenset({"السعودي", "الاماراتي"}),
    frozenset({"السعودي", "السوري"}),
    frozenset({"السعودي", "العماني"}),
    frozenset({"المصري", "الليبي"}),
    frozenset({"المصري", "القطري"}),
    frozenset({"المصري", "السوداني"}),
    frozenset({"السكندري", "السعودي"}),
    frozenset({"السكندري", "الليبي"}),
    frozenset({"العراقي", "السوري"}),
    frozenset({"العراقي", "التوغولي"}),
    frozenset({"الأردني", "السعودي"}),
    frozenset({"طرابلس", "بنغازي"}),
    frozenset({"جدة", "السكندري"}),
    frozenset({"جدة", "دبي"}),
    frozenset({"مستغانم", "التونسي"}),
    frozenset({"مستغانم", "تونسي"}),
    frozenset({"ميلان", "ميامي"}),
    frozenset({"يونيون", "كولون"}),
}

# ---------------------------------------------------------------------------
# English name matching constants
# ---------------------------------------------------------------------------

# Organisational noise stripped before comparison — these carry no identity information
_ENGLISH_ORG_NOISE = re.compile(
    r'\b(?:1\.\s*fc|1\.\s*fsv|fc|cf|cfc|uc|afc|sc|ac|cd|sd|ud|tc|bsc|tsg|vfb|vfl|sv|ssv|fsv|rb|rsc|rc|cs|us|ogc'
    r'|bc|sk|jk|sfc|acb|ca|rcd|sl|as|ss|ssc|acf|losc|bvb|sco|hsc|aj|cr|se|deportivo|rasenballsport'
    r'|alsace|paulista|spvgg|kv|ksv|af|ea|sm|foot|praia|kulubu|balompie|amadora|fbpa|fr|ec|gd|pec|rkc|krc|kaa'
    r'|sad|va|eh|fk|fbc|sa|csd|scp|clube\s+de|sporting\s+clube\s+de'
    r'|balompie|sportive\s+de|club|football\s+club|de\s+f[uú]tbol|calcio|associazione\s+calcio|\d{2,4})\b',
    re.IGNORECASE
)

# Generic words that can appear as organizational prefixes but shouldn't alone distinguish a club
_GENERIC_ENGLISH_TOKENS = {
    "borussia", "eintracht", "olympique", "stade", "sporting", "real", "inter",
    "athletic", "atletico", "bayer", "bayern", "club", "de", "the", "al", "el",
    "sc", "ac", "fc", "cf", "san", "saint", "los", "las", "la", "le", "hotspur",
    "wanderers", "town", "albion", "city", "united", "casablanca",
    "tunis", "eindhoven", "rotterdam", "manchester", "hellas", "1909", "1899", "04", "05", "1846",
    "orient", "argyle", "county", "north", "end", "wednesday", "hove", "eagles", "stars", "crew",
    "galaxy", "red", "bulls", "plata", "almagro", "union", "sport", "rovers", "fortuna", "wehen",
    "lavallois", "quevilly", "rouen", "chaves", "sittard", "heracles", "waalwijk", "cleopatra",
    "gaish", "hodoud", "mahalla", "diaraf", "yaounde", "douala", "esperanca", "pr", "old", "boys",
    "kobe", "kawasaki", "hiroshima", "kashima", "nagoya", "machida", "niigata", "kashiwa", "kyoto",
    "pohang", "suwon", "tashkent", "namangan", "qarshi", "sydney", "melbourne", "brisbane",
    "adelaide", "wellington", "auckland", "dresden", "munster", "regensburg", "ferrara", "leonesa",
    "pescara", "delfino", "vicenza", "ceuta", "niort", "niortais", "chamois", "tilburg", "breda",
    "westerlo", "dender", "rizespor", "caykur", "gaziantep", "bodrum", "portland", "vancouver",
    "cultural", "ad", "lr", "preussen", "ssv", "jahn", "dynamo", "nac", "kvc", "fcv", "ts",
    "golden", "kano", "bendel", "fasil", "deportes", "universidad"
}

# If both names contain different words from any one of these pairs, they are DIFFERENT clubs.
_CONFLICTING_MODIFIER_PAIRS: set[frozenset] = {
    frozenset({"city", "united"}),
    frozenset({"city", "wanderers"}),
    frozenset({"city", "rovers"}),
    frozenset({"milan", "miami"}),
    frozenset({"milan", "inter"}),
    frozenset({"real", "atletico"}),
    frozenset({"madrid", "sociedad"}),
    frozenset({"madrid", "betis"}),
    frozenset({"madrid", "valladolid"}),
    frozenset({"madrid", "zaragoza"}),
    frozenset({"madrid", "salt"}),
    frozenset({"cp", "gijon"}),
    frozenset({"cp", "braga"}),
    frozenset({"cp", "kansas"}),
    frozenset({"lisbon", "braga"}),
    frozenset({"women", "castilla"}),
    frozenset({"women", "ii"}),
    frozenset({"castilla", "ii"}),
    frozenset({"femeni", "atletic"}),
    frozenset({"femenino", "castilla"}),
    frozenset({"rangers", "celtic"}),
    frozenset({"dortmund", "monchengladbach"}),
    frozenset({"leverkusen", "munchen"}),
    frozenset({"leverkusen", "munich"}),
    frozenset({"kyiv", "zagreb"}),
    frozenset({"kyiv", "moscow"}),
    frozenset({"kyiv", "houston"}),
    frozenset({"leipzig", "salzburg"}),
    frozenset({"leipzig", "york"}),
    frozenset({"barcelona", "guayaquil"}),
}

# Well-known short names / acronyms → canonical normalized English name.
# Resource path resolution
_RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
_SYNONYMS_FILE = _RESOURCES_DIR / "canonical_synonyms.json"


def _load_canonical_synonyms() -> dict[str, str | None]:
    """Loads external canonical team synonyms dictionary from JSON resource."""
    if _SYNONYMS_FILE.exists():
        try:
            with open(_SYNONYMS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# Well-known short names / acronyms → canonical normalized English name.
_CANONICAL_SYNONYMS: dict[str, str | None] = _load_canonical_synonyms()


def set_canonical_synonyms(synonyms: dict[str, str]):
    """Updates the in-memory canonical synonyms dictionary from the Google Sheets cache."""
    global _CANONICAL_SYNONYMS
    if synonyms:
        _CANONICAL_SYNONYMS = {k.strip().lower(): v for k, v in synonyms.items() if k and v}


# ---------------------------------------------------------------------------
# Arabic helpers
# ---------------------------------------------------------------------------

def strip_arabic_diacritics_and_noise(text: str) -> str:
    """Strips tashkeel (diacritics), tatweel/kashida, and zero-width characters."""
    if not text:
        return ""
    t = re.sub(r'[\u064B-\u065F\u0670\u0640]', '', text)
    t = re.sub(r'[\u200B-\u200F\u202A-\u202E\uFEFF]', '', t)
    return t


def strip_club_affixes(text: str) -> str:
    """Removes non-distinguishing organisational prefixes and suffixes from Arabic club names."""
    if not text:
        return ""
    t = text.strip()
    changed = True
    while changed:
        before = t
        for p in _ARABIC_PREFIXES:
            t = re.sub(p, '', t, flags=re.IGNORECASE).strip()
        changed = (t != before)

    changed = True
    while changed:
        before = t
        for s in _ARABIC_SUFFIXES:
            t = re.sub(s, '', t, flags=re.IGNORECASE).strip()
        changed = (t != before)

    # Strip internal non-identifying noise words (e.g. 'الرياضي' inside 'النجم الرياضي الساحلي')
    t = re.sub(r'\b(?:الرياضي|الرياضية|رياضي|نادي|فريق)\b', '', t).strip()
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
        # Strip Arabic definite article prefix Al- (e.g. الترجي -> ترجي, الأهلي -> اهلي)
        t = re.sub(r'\bال(?=[\u0600-\u06FF]{3,})', '', t)

        # 1. Consonant equivalence (Egyptian/Levantine ج vs Gulf/Maghrebi غ / ق)
        t = re.sub(r'[غق]', 'ج', t)

        # 2. Transliteration sound shifts
        t = re.sub(r'تس', 'س', t)
        t = re.sub(r'تش', 'ش', t)

        # 3. Soften optional internal vowels
        t = re.sub(r'(?<=[\u0600-\u06FF])ي(?=[\u0600-\u06FF]{1,})', '', t)

        # 4. Trailing Arabic nisba adjective ي
        if len(t) > 4 and t.endswith('ي'):
            t = t[:-1].strip()

    return t


def get_arabic_match_fingerprint(text: str) -> str:
    """Returns a canonical normalized phonetic fingerprint for Arabic team comparison."""
    return normalize_arabic_text(text, aggressive=True)


def _has_arabic_conflicting_qualifiers(text1: str, text2: str) -> bool:
    """Checks if two Arabic names contain conflicting geographic or identity qualifiers."""
    t1 = strip_arabic_diacritics_and_noise(text1)
    t2 = strip_arabic_diacritics_and_noise(text2)
    t1 = re.sub(r'[إأآٱ]', 'ا', t1)
    t1 = re.sub(r'[ىي]', 'ي', t1)
    t2 = re.sub(r'[إأآٱ]', 'ا', t2)
    t2 = re.sub(r'[ىي]', 'ي', t2)
    tokens1 = set(t1.split())
    tokens2 = set(t2.split())

    # Dundee FC vs Dundee United guard in Arabic
    if ("يونايتد" in tokens1 and "دندي" in tokens1 and "يونايتد" not in tokens2) or \
       ("يونايتد" in tokens2 and "دندي" in tokens2 and "يونايتد" not in tokens1):
        return True

    for pair in _ARABIC_CONFLICTING_PAIRS:
        p_list = list(pair)
        w1, w2 = p_list[0], p_list[1]
        if (w1 in tokens1 and w2 in tokens2 and w1 not in tokens2) or \
           (w2 in tokens1 and w1 in tokens2 and w2 not in tokens2):
            return True

    # Check Inter vs Milan in Arabic
    if ("انتر" in tokens1 or "إنتر" in tokens1) and ("ميلان" in tokens2 and "انتر" not in tokens2 and "إنتر" not in tokens2):
        return True
    if ("انتر" in tokens2 or "إنتر" in tokens2) and ("ميلان" in tokens1 and "انتر" not in tokens1 and "إنتر" not in tokens1):
        return True

    return False


def are_arabic_names_equivalent(name1: str, name2: str) -> bool:
    """
    Determines if two Arabic team name strings represent the same team,
    accounting for dialect differences, affixes, and transliteration variations.
    """
    if not name1 or not name2:
        return False

    n1_clean = name1.strip()
    n2_clean = name2.strip()

    # Guard: conflicting country / regional / club qualifiers
    if _has_arabic_conflicting_qualifiers(n1_clean, n2_clean):
        return False

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

    # Level 4: Set equality of distinctive non-generic words
    if fp1 and fp2:
        words1 = [w for w in fp1.split() if w not in _GENERIC_CLUB_WORDS]
        words2 = [w for w in fp2.split() if w not in _GENERIC_CLUB_WORDS]
        if words1 and words2 and set(words1) == set(words2):
            return True

        tokens1 = set(fp1.split())
        tokens2 = set(fp2.split())
        if tokens1 and tokens2 and tokens1 == tokens2:
            if not _has_arabic_conflicting_qualifiers(n1_clean, n2_clean):
                return True

        # Generic Latin affixes transcribed into Arabic: يونايتد, هوتسبير, سيتي, ريال
        _ar_generics = {"يوناتد", "يونايتد", "رال", "ريال", "هوسبر", "هوتسبر", "هوتسبير", "ستي", "سيتي", "سبورتنج", "سبورتينج", "سبورتينغ"}
        distinct1 = {t for t in tokens1 if t not in _ar_generics}
        distinct2 = {t for t in tokens2 if t not in _ar_generics}
        if distinct1 and distinct2 and distinct1 == distinct2:
            if not _has_arabic_conflicting_qualifiers(n1_clean, n2_clean):
                return True

    # Level 5: High similarity ratio on aggressive fingerprints (> 0.82)
    if fp1 and fp2:
        words1 = fp1.split()
        words2 = fp2.split()
        if len(words1) == len(words2):
            ratio = difflib.SequenceMatcher(None, fp1, fp2).ratio()
            if ratio >= 0.82:
                return True

    return False


# ---------------------------------------------------------------------------
# English helpers
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    """Removes accents and diacritics from Latin characters (e.g. Ferencváros -> Ferencvaros)."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def _normalize_english_for_match(name: str) -> str:
    """
    Produces a canonical lowercase token string for English team name comparison.
    Strips org-noise abbreviations, accents, punctuation, and applies known synonyms.
    """
    if not name:
        return ""
    t = _strip_accents(name).lower().strip()
    # Normalize abbreviations: utd -> united
    t = re.sub(r'\butd\b', 'united', t)
    # Normalize dotted acronyms: U.D. -> ud, F.C. -> fc, A.F.C. -> afc, 1. FC -> fc, F. Marinos -> Marinos
    t = re.sub(r'\b1\.\s*', '', t)
    t = re.sub(r'\b([a-z])\.([a-z])\.(?:([a-z])\.)?', r'\1\2\3', t)
    t = re.sub(r"[.\-_/|',]", " ", t)
    t = re.sub(r'\s+', ' ', t).strip()

    # Apply synonym lookup on full phrase first
    synonym = _CANONICAL_SYNONYMS.get(t)
    if synonym is not None:
        t = synonym

    # Strip organizational noise
    t = _ENGLISH_ORG_NOISE.sub('', t)
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()

    # Re-apply synonym lookup after noise strip
    synonym_after = _CANONICAL_SYNONYMS.get(t)
    if synonym_after is not None:
        return synonym_after
    return t


def normalize_english_team(name: str) -> str:
    """Public alias for _normalize_english_for_match."""
    return _normalize_english_for_match(name)


def _has_conflicting_modifiers(tokens1: set[str], tokens2: set[str], raw1: str = "", raw2: str = "") -> bool:
    """
    Returns True if the two token sets each contain a different word from any conflicting pair.
    """
    r1 = (raw1 or "").strip().lower()
    r2 = (raw2 or "").strip().lower()

    # Guard against Barcelona (Spain) vs Barcelona SC (Ecuador)
    is_ec1 = "barcelona sc" in r1 or "guayaquil" in r1
    is_ec2 = "barcelona sc" in r2 or "guayaquil" in r2
    if is_ec1 and not is_ec2 and "barcelona" in r2.split():
        return True
    if is_ec2 and not is_ec1 and "barcelona" in r1.split():
        return True

    # Guard against Dundee FC vs Dundee United
    if ("dundee fc" in r1 and "dundee united" in r2) or \
       ("dundee fc" in r2 and "dundee united" in r1):
        return True

    # Prevent Real Madrid vs Atletico Madrid
    has_real1 = "real" in tokens1 or "real" in r1.split()
    has_real2 = "real" in tokens2 or "real" in r2.split()
    has_atm1 = "atletico" in tokens1 or "atletico" in r1.split() or "atlético" in r1.split()
    has_atm2 = "atletico" in tokens2 or "atletico" in r2.split() or "atlético" in r2.split()
    if (has_real1 and has_atm2) or (has_real2 and has_atm1):
        return True

    # Prevent AC Milan vs Inter Milan
    has_inter1 = "inter" in tokens1 or "internazionale" in tokens1 or "inter" in r1.split()
    has_inter2 = "inter" in tokens2 or "internazionale" in tokens2 or "inter" in r2.split()
    has_milan1 = "milan" in tokens1
    has_milan2 = "milan" in tokens2
    if (has_milan1 and has_milan2) and (has_inter1 != has_inter2):
        return True

    # Prevent standalone generic word matching specific club (e.g. Manchester vs Manchester City)
    for g in ("manchester", "inter", "real", "sporting", "atletico", "athletic"):
        if (g in tokens1 and len(tokens1) == 1 and len(tokens2) > 1) or \
           (g in tokens2 and len(tokens2) == 1 and len(tokens1) > 1):
            return True

    for pair in _CONFLICTING_MODIFIER_PAIRS:
        pair_list = list(pair)
        a, b = pair_list[0], pair_list[1]
        if (a in tokens1 and b in tokens2 and a not in tokens2) or \
           (b in tokens1 and a in tokens2 and b not in tokens2):
            return True
    return False


def are_english_teams_equivalent(name1: str, name2: str) -> bool:
    """
    Checks if two English team names refer to the same club.
    """
    if not name1 or not name2:
        return False

    # Level 0: exact match
    if name1.strip().lower() == name2.strip().lower():
        return True

    norm1 = _normalize_english_for_match(name1)
    norm2 = _normalize_english_for_match(name2)

    tokens1 = set(norm1.split()) if norm1 else set()
    tokens2 = set(norm2.split()) if norm2 else set()

    # Level 1: conflict detection MUST precede equality check
    if _has_conflicting_modifiers(tokens1, tokens2, name1, name2):
        return False

    # Level 2: normalized strings are identical
    if norm1 and norm2 and norm1 == norm2:
        return True

    # Level 3: full set equality of all tokens
    if tokens1 and tokens2 and tokens1 == tokens2:
        if any(len(tok) >= 3 for tok in tokens1):
            return True

    # Level 4: distinctive non-generic tokens set equality
    # (e.g. "Dortmund" vs "Borussia Dortmund", "Roma" vs "AS Roma", "Benfica" vs "SL Benfica")
    distinct1 = {t for t in tokens1 if t not in _GENERIC_ENGLISH_TOKENS}
    distinct2 = {t for t in tokens2 if t not in _GENERIC_ENGLISH_TOKENS}
    if distinct1 and distinct2 and distinct1 == distinct2:
        if any(len(tok) >= 3 for tok in distinct1):
            return True

    return False


def slugify_team_name(name: str) -> str:
    """
    Converts an English team name into a URL-safe slug for use as part of event_id.
    'Manchester City' -> 'manchester-city', 'Paris Saint-Germain' -> 'paris-saint-germain'.
    """
    if not name or name.strip().lower() in ("unknown", ""):
        return "unk"
    t = _strip_accents(name).lower().strip()
    t = re.sub(r"[.\-_/|',]", " ", t)
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', '-', t).strip('-')
    return t or "unk"
