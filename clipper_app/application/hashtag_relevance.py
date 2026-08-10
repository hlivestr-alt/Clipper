from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Iterable


logger = logging.getLogger(__name__)

MAX_RELEVANT_HASHTAGS = 30
TIKTOK_TREND_SOURCE = "tiktok_discovery_trending"


# Topic signals intentionally remain separate from brand names. Hashtag terms are
# compared with a punctuation-free form so compounds such as #VitaminCSerum and
# #night_skincare_routine work without making brand recognition substring-based.
TOPIC_KEYWORDS = (
    "skincare", "skinhealth", "skinwellness", "skinbarrier", "barrierrepair",
    "facecare", "facialcare", "dermatology", "dermatologist", "esthetician",
    "aesthetician", "glassskin", "glowingskin", "healthyskin", "dewyskin",
    "beauty", "beautytip", "beautyhacks", "makeup", "cosmetic", "cosmetology",
    "foundation", "concealer", "blush", "eyeshadow", "eyeliner", "mascara",
    "lipstick", "lipgloss", "eyebrow", "browmakeup", "browtutorial",
    "browlamination", "bronzer", "highlighter", "makeuptutorial", "makeupreview",
    "nailcare", "manicure", "pedicure", "personalcare", "selfcare", "bodycare",
    "haircare", "scalpcare", "oralcare", "dentalcare", "toothcare",
    "femininecare", "grooming", "fragrance", "perfume", "perawatanwajah",
    "perawatandiri", "perawatanrambut", "kecantikan", "kecantikankulit", "riasan",
)

SKIN_CONCERN_KEYWORDS = (
    "acne", "pimple", "blemish", "breakout", "blackhead", "whitehead",
    "darkspot", "sunspot", "hyperpigmentation", "pigmentation", "melasma",
    "largepores", "openpores", "wrinkle", "finelines", "antiaging", "antiageing",
    "dryskin", "dehydratedskin", "oilyskin", "sensitiveskin", "dullskin",
    "unevenskintone", "skintexture", "redness", "rosacea", "eczema", "psoriasis",
    "jerawat", "komedo", "flekhitam", "kulitkusam", "kulitkering",
    "kulitberminyak", "kulitsensitif", "bekasjerawat",
)

PRODUCT_KEYWORDS = (
    "serum", "cleanser", "facewash", "facialwash", "toner", "moisturizer",
    "moisturiser", "sunscreen", "sunblock", "spf", "facemask", "sheetmask",
    "claymask", "sleepingmask", "exfoliant", "exfoliator", "essence", "ampoule",
    "micellarwater", "cleansingbalm", "cleansingoil", "eyecream", "spotcream",
    "acnepatch", "pimplepatch", "lipcare", "lipbalm", "bodywash", "bodylotion",
    "handcream", "shampoo", "conditioner", "deodorant", "antiperspirant",
    "pelembap", "tabirsurya", "pembersihwajah", "serumwajah",
)

INGREDIENT_KEYWORDS = (
    "vitaminc", "ascorbicacid", "niacinamide", "retinol", "retinaldehyde",
    "retinal", "retinoid", "hyaluronicacid", "peptide", "ceramide",
    "salicylicacid", "glycolicacid", "lacticacid", "azelaicacid", "mandelicacid",
    "kojicacid", "tranexamicacid", "benzoylperoxide", "alphahydroxyacid",
    "betahydroxyacid", "polyhydroxyacid", "bakuchiol", "squalane", "centella",
    "cica", "snailmucin", "mucin", "collagen", "zinc", "sulfur", "adapalene",
    "tretinoin",
)

ROUTINE_KEYWORDS = (
    "skincareroutine", "makeuproutine", "morningroutine", "amroutine",
    "nighttimeroutine", "nightroutine", "pmroutine", "skinprep",
    "doublecleansing", "slugging",
)

TREATMENT_KEYWORDS = (
    "facial", "facialtreatment", "chemicalpeel", "microneedling",
    "microdermabrasion", "dermaplaning", "hydrafacial", "ledtherapy",
    "lighttherapy", "guasha", "faceyoga",
)


# Canonical compact brand names. Recognition is exact, or the canonical name may
# be followed by one explicitly allowed account/market suffix. This avoids broad
# checks such as ``brand in hashtag`` that would accept unrelated company names.
BEAUTY_BRAND_NAMES: dict[str, str] = {
    "azarine": "beauty_brand",
    "aveeno": "personal_care_brand",
    "avoskin": "beauty_brand",
    "cerave": "beauty_brand",
    "cosrx": "beauty_brand",
    "dazzleme": "beauty_brand",
    "emina": "beauty_brand",
    "facetology": "beauty_brand",
    "focallure": "beauty_brand",
    "garnier": "beauty_brand",
    "glad2glow": "beauty_brand",
    "hanasui": "beauty_brand",
    "implora": "beauty_brand",
    "larocheposay": "beauty_brand",
    "makarizo": "personal_care_brand",
    "msglow": "beauty_brand",
    "nivea": "personal_care_brand",
    "npure": "beauty_brand",
    "proya": "beauty_brand",
    "raecca": "beauty_brand",
    "scarlettwhitening": "beauty_brand",
    "scora": "beauty_brand",
    "skintific": "beauty_brand",
    "somethinc": "beauty_brand",
    "sunsilk": "personal_care_brand",
    "theoriginote": "beauty_brand",
    "vaseline": "personal_care_brand",
    "velixir": "personal_care_brand",
    "vitalis": "personal_care_brand",
    "wardah": "beauty_brand",
}

BRAND_VARIATION_SUFFIXES = frozenset({
    "id", "indo", "indonesia", "official", "officialid", "officialindonesia",
    "beauty", "cosmetics", "skincare", "makeup", "serum", "haircare",
})


_EXCLUDED_TERMS: dict[str, tuple[str, ...]] = {
    "gaming": (
        "gaming", "gamer", "gameplay", "videogame", "esports", "fortnite",
        "minecraft", "roblox", "valorant", "pubg", "freefire", "callofduty",
        "mobilelegends",
    ),
    "sports": (
        "football", "soccer", "basketball", "baseball", "volleyball", "badminton",
        "tennis", "cricket", "rugby", "hockey", "boxing", "wrestling", "motogp",
        "formulaone", "premierleague", "championsleague", "worldcup", "nba", "nfl",
    ),
    "politics": (
        "politics", "political", "election", "parliament", "congress", "campaign",
        "government", "president", "democrat", "republican", "geopolitics",
    ),
    "entertainment": (
        "celebrity", "movie", "cinema", "tvshow", "netflix", "anime", "manga",
        "musicvideo", "newmusic", "concert", "boxoffice", "filmreview", "kpop",
    ),
    "food": (
        "food", "foodie", "recipe", "cooking", "baking", "restaurant",
        "streetfood", "mukbang", "dessert", "coffee", "lunch", "dinner",
        "breakfast",
    ),
    "vehicles": (
        "automotive", "vehicle", "carsoftiktok", "carreview", "supercar",
        "sportscar", "motorcycle", "motorbike", "truck", "offroad",
        "cardetailing", "formulaone",
    ),
    "general_meme": (
        "meme", "funny", "comedy", "prank", "viralvideo", "fyp", "foryoupage",
        "dancechallenge", "challenge", "reactionvideo", "relatable",
    ),
}

_EXACT_RELEVANT: dict[str, str] = {
    "skin": "topic",
    "pores": "skin_concern",
}

_KEYWORD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("routine", ROUTINE_KEYWORDS),
    ("treatment", TREATMENT_KEYWORDS),
    ("skin_concern", SKIN_CONCERN_KEYWORDS),
    ("ingredient", INGREDIENT_KEYWORDS),
    ("product", PRODUCT_KEYWORDS),
    ("topic", TOPIC_KEYWORDS),
)


@dataclass(frozen=True)
class HashtagClassification:
    hashtag: str
    relevant: bool
    category: str
    reason: str
    matched_term: str | None = None
    normalized_name: str = ""
    matched_brand: str | None = None
    source: str = TIKTOK_TREND_SOURCE
    source_category: str = ""
    original_rank: int = 0
    display_rank: int | None = None

    @property
    def relevance_type(self) -> str:
        return self.category


@dataclass(frozen=True)
class HashtagFilterResult:
    selected: list[dict[str, Any]]
    classifications: list[HashtagClassification]
    total_count: int
    relevant_count: int
    topical_count: int
    brand_count: int
    excluded_count: int
    deduplicated_count: int

    @property
    def exclusions(self) -> list[HashtagClassification]:
        return [item for item in self.classifications if not item.relevant]


def normalize_hashtag_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lstrip("#")
    return re.sub(r"[^a-z0-9]+", "", normalized.casefold())


def _rank_position(item: dict[str, Any]) -> int:
    for key in ("original_rank", "rank_position"):
        try:
            rank = int(item.get(key) or 0)
        except (TypeError, ValueError):
            rank = 0
        if rank > 0:
            return rank
    return 0


def _match_brand(compact: str) -> tuple[str, str] | None:
    for brand in sorted(BEAUTY_BRAND_NAMES, key=len, reverse=True):
        if compact == brand:
            return brand, BEAUTY_BRAND_NAMES[brand]
        if compact.startswith(brand) and compact[len(brand):] in BRAND_VARIATION_SUFFIXES:
            return brand, BEAUTY_BRAND_NAMES[brand]
    return None


class HashtagRelevanceClassifier:
    """Classify TikTok hashtag names without coupling to the discovery client."""

    def classify(self, hashtag: Any) -> HashtagClassification:
        original = str(hashtag or "").strip()
        compact = normalize_hashtag_name(original)
        if not compact:
            return HashtagClassification(
                original, False, "invalid", "empty hashtag name", normalized_name=compact
            )

        brand_match = _match_brand(compact)
        if brand_match:
            brand, relevance_type = brand_match
            return HashtagClassification(
                original,
                True,
                relevance_type,
                f"matched beauty/personal-care brand '{brand}' by exact name or approved suffix",
                brand,
                normalized_name=compact,
                matched_brand=brand,
            )

        exact_category = _EXACT_RELEVANT.get(compact)
        if exact_category:
            return HashtagClassification(
                original,
                True,
                exact_category,
                f"matched {exact_category} term '{compact}'",
                compact,
                normalized_name=compact,
            )

        for category, terms in _KEYWORD_GROUPS:
            match = next((term for term in terms if term in compact), None)
            if match:
                return HashtagClassification(
                    original,
                    True,
                    category,
                    f"matched {category} term '{match}'",
                    match,
                    normalized_name=compact,
                )

        for category, terms in _EXCLUDED_TERMS.items():
            match = next((term for term in terms if term in compact), None)
            if match:
                return HashtagClassification(
                    original,
                    False,
                    category,
                    f"matched unrelated {category} term '{match}'",
                    match,
                    normalized_name=compact,
                )

        return HashtagClassification(
            original,
            False,
            "unclassified",
            "no skincare, beauty, cosmetics, personal-care, or recognized-brand signal",
            normalized_name=compact,
        )


def filter_relevant_hashtags(
    hashtags: Iterable[dict[str, Any]],
    *,
    limit: int = MAX_RELEVANT_HASHTAGS,
    classifier: HashtagRelevanceClassifier | None = None,
    emit_diagnostics: bool = True,
    default_source: str = TIKTOK_TREND_SOURCE,
    default_source_category: str = "",
) -> HashtagFilterResult:
    """Return unique relevant hashtags without comparing ranks across sources.

    The primary source/category is ordered first. Additional sources retain their
    first-seen source order, and rank is used only within each source.
    """

    source = [dict(item) for item in hashtags]
    relevance = classifier or HashtagRelevanceClassifier()
    primary_key = (default_source, default_source_category)
    source_priorities: dict[tuple[str, str], int] = {primary_key: 0}

    def source_metadata(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("source") or default_source),
            str(item.get("source_category") or default_source_category),
        )

    for item in source:
        key = source_metadata(item)
        if key not in source_priorities:
            source_priorities[key] = len(source_priorities)

    ranked = sorted(
        enumerate(source),
        key=lambda pair: (
            source_priorities[source_metadata(pair[1])],
            _rank_position(pair[1]) <= 0,
            _rank_position(pair[1]),
            pair[0],
        ),
    )
    classifications: list[HashtagClassification] = []
    unique_relevant: list[tuple[dict[str, Any], int]] = []
    normalized_names: set[str] = set()
    deduplicated_count = 0

    for _source_index, item in ranked:
        source_name, source_category = source_metadata(item)
        classification = replace(
            relevance.classify(item.get("hashtag_name")),
            source=source_name,
            source_category=source_category,
            original_rank=_rank_position(item),
        )
        classification_index = len(classifications)
        classifications.append(classification)
        if emit_diagnostics:
            logger.debug(
                "TikTok hashtag classified: hashtag=#%s normalized=%s source=%s category_source=%s "
                "rank=%s relevant=%s category=%s brand=%s reason=%s",
                classification.hashtag,
                classification.normalized_name,
                classification.source,
                classification.source_category,
                classification.original_rank,
                classification.relevant,
                classification.category,
                classification.matched_brand,
                classification.reason,
            )
        if not classification.relevant:
            continue
        if classification.normalized_name in normalized_names:
            deduplicated_count += 1
            continue
        normalized_names.add(classification.normalized_name)
        unique_relevant.append((item, classification_index))

    capped_limit = max(0, min(MAX_RELEVANT_HASHTAGS, int(limit)))
    selected: list[dict[str, Any]] = []
    for display_rank, (item, classification_index) in enumerate(
        unique_relevant[:capped_limit], start=1
    ):
        classification = replace(classifications[classification_index], display_rank=display_rank)
        classifications[classification_index] = classification
        enriched = dict(item)
        enriched.update({
            "normalized_name": classification.normalized_name,
            "source": classification.source,
            "source_category": classification.source_category,
            "original_rank": classification.original_rank,
            "display_rank": display_rank,
            "relevance_type": classification.relevance_type,
            "matched_brand": classification.matched_brand,
            "classification_reason": classification.reason,
        })
        selected.append(enriched)

    topical_count = sum(
        classifications[index].relevant
        and classifications[index].category not in {"beauty_brand", "personal_care_brand"}
        for _item, index in unique_relevant
    )
    brand_count = len(unique_relevant) - topical_count
    excluded_count = sum(not item.relevant for item in classifications)
    result = HashtagFilterResult(
        selected=selected,
        classifications=classifications,
        total_count=len(source),
        relevant_count=len(unique_relevant),
        topical_count=topical_count,
        brand_count=brand_count,
        excluded_count=excluded_count,
        deduplicated_count=deduplicated_count,
    )

    if emit_diagnostics:
        logger.info(
            "TikTok hashtag relevance filter: retrieved=%d topical=%d brands=%d excluded=%d "
            "deduplicated=%d returned=%d limit=%d",
            result.total_count,
            result.topical_count,
            result.brand_count,
            result.excluded_count,
            result.deduplicated_count,
            len(result.selected),
            capped_limit,
        )
    return result
