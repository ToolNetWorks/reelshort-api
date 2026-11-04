"""Confidence scoring for language-edition matches.

IMPORTANT POLICY
----------------
A ``book_id`` is **never** derived by mutating ``t_book_id`` (or any other id).
De-duplicated identity signals are only used to *score* candidates that were
independently observed in the official catalogue/search of their own locale.
Every match keeps its evidence so the mapping stays auditable and reversible.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

STATUS_VERIFIED = "VERIFIED"
STATUS_CANDIDATE = "CANDIDATE"
STATUS_UNKNOWN = "UNKNOWN"

# Weights add up to a little over 1.0; the final score is clamped to [0, 1].
W_T_BOOK_ID = 0.40
W_T_BOOK_TAIL = 0.08
W_TAG_JACCARD = 0.25
W_CHAPTER_COUNT = 0.15
W_DUB_RELATION = 0.10
W_TITLE = 0.15
W_GENRE = 0.05
W_PUBLISH_PROXIMITY = 0.05

PUBLISH_WINDOW_SECONDS = 120 * 24 * 3600


@dataclass
class EditionFacts:
    book_id: str
    lang: str
    book_title: str
    t_book_id: str | None = None
    s_book_id: int | None = None
    chapter_count: int | None = None
    tag_ids: list[str] = field(default_factory=list)
    tag_names: list[str] = field(default_factory=list)
    book_genre: int | None = None
    book_source: int | None = None
    is_dub: bool | None = None
    original_lang: str | None = None
    publish_at: int | None = None
    known_titles: list[str] = field(default_factory=list)
    source: str = "unknown"


def t_book_tail(t_book_id: str | None, digits: int = 10) -> str | None:
    if not t_book_id:
        return None
    cleaned = "".join(ch for ch in str(t_book_id) if ch.isdigit())
    if len(cleaned) < digits + 4:
        return None
    return cleaned[-digits:]


def tag_jaccard(a: list[str], b: list[str]) -> float | None:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return None
    return len(set_a & set_b) / len(set_a | set_b)


def title_similarity(source_title: str, candidates: list[str]) -> float:
    """Best fuzzy similarity of ``source_title`` against any candidate title."""
    from app.crawler.parser import normalize_title

    norm_source = normalize_title(source_title)
    best = 0.0
    for candidate in candidates:
        norm_candidate = normalize_title(candidate)
        if not norm_candidate or not norm_source:
            continue
        best = max(best, difflib.SequenceMatcher(None, norm_source, norm_candidate).ratio())
    return round(best, 4)


def score_candidate(source: EditionFacts, target: EditionFacts) -> tuple[float, dict[str, Any]]:
    """Return ``(confidence, evidence)`` for a proposed source→target match."""
    evidence: dict[str, Any] = {"source_book_id": source.book_id, "target_book_id": target.book_id}
    score = 0.0

    if source.lang == target.lang:
        return 0.0, {**evidence, "rejected": "same_locale"}

    if source.t_book_id and target.t_book_id and source.t_book_id == target.t_book_id:
        score += W_T_BOOK_ID
        evidence["t_book_id_equal"] = True
    else:
        source_tail, target_tail = t_book_tail(source.t_book_id), t_book_tail(target.t_book_id)
        if source_tail and target_tail and source_tail == target_tail:
            score += W_T_BOOK_TAIL
            evidence["t_book_tail_equal"] = source_tail

    jaccard = tag_jaccard(source.tag_ids, target.tag_ids)
    if jaccard is not None:
        score += W_TAG_JACCARD * jaccard
        evidence["tag_ids_jaccard"] = round(jaccard, 4)
        evidence["tag_ids_checked"] = {"source": len(source.tag_ids), "target": len(target.tag_ids)}

    if source.chapter_count and target.chapter_count:
        if source.chapter_count == target.chapter_count:
            score += W_CHAPTER_COUNT
            evidence["chapter_count_equal"] = source.chapter_count
        else:
            boundary = max(source.chapter_count, target.chapter_count)
            if boundary and abs(source.chapter_count - target.chapter_count) <= 2:
                score += W_CHAPTER_COUNT * 0.5
                evidence["chapter_count_close"] = [source.chapter_count, target.chapter_count]

    dub_relation = False
    if target.is_dub and target.original_lang and target.original_lang == source.lang:
        dub_relation = True
    if source.is_dub and source.original_lang and source.original_lang == target.lang:
        dub_relation = True
    if dub_relation:
        score += W_DUB_RELATION
        evidence["official_dub_relation"] = {
            "target_is_dub": bool(target.is_dub),
            "target_original_lang": target.original_lang,
            "source_lang": source.lang,
        }

    similarity = max(
        title_similarity(target.book_title, source.known_titles),
        title_similarity(source.book_title, target.known_titles),
    )
    if similarity:
        score += W_TITLE * similarity
        evidence["title_similarity"] = similarity

    if source.book_genre is not None and source.book_genre == target.book_genre:
        score += W_GENRE * 0.5
        evidence["book_genre"] = source.book_genre
    if source.book_source is not None and source.book_source == target.book_source:
        score += W_GENRE * 0.5
        evidence["book_source"] = source.book_source

    if source.publish_at and target.publish_at:
        delta = abs(source.publish_at - target.publish_at)
        if delta <= PUBLISH_WINDOW_SECONDS:
            score += W_PUBLISH_PROXIMITY
            evidence["publish_delta_days"] = round(delta / 86400, 1)

    confidence = round(min(score, 1.0), 4)
    evidence["score_breakdown"] = {
        "t_book_id": W_T_BOOK_ID if evidence.get("t_book_id_equal") else 0.0,
        "t_book_tail": W_T_BOOK_TAIL if evidence.get("t_book_tail_equal") else 0.0,
        "tags": round(W_TAG_JACCARD * (jaccard or 0.0), 4),
        "chapter_count": W_CHAPTER_COUNT if evidence.get("chapter_count_equal") else 0.0,
        "dub_relation": W_DUB_RELATION if dub_relation else 0.0,
        "title": round(W_TITLE * similarity, 4),
    }
    evidence["confidence"] = confidence
    return confidence, evidence


def classify(
    confidence: float,
    evidence: dict[str, Any],
    *,
    min_confidence: float = 0.5,
    auto_verify_confidence: float = 0.8,
) -> str:
    """Classify a scored candidate as VERIFIED / CANDIDATE / UNKNOWN."""
    if confidence >= auto_verify_confidence and _has_hard_evidence(evidence, auto_verify_confidence):
        return STATUS_VERIFIED
    if confidence >= min_confidence:
        return STATUS_CANDIDATE
    return STATUS_UNKNOWN


def _has_hard_evidence(evidence: dict[str, Any], auto_verify_confidence: float) -> bool:
    """Auto-verification needs deterministic, official evidence — not vibes."""
    breakdown = evidence.get("score_breakdown", {})
    exact_id = bool(evidence.get("t_book_id_equal"))
    tags_full = breakdown.get("tags", 0) >= W_TAG_JACCARD
    count_equal = bool(evidence.get("chapter_count_equal"))
    dub_relation = bool(evidence.get("official_dub_relation")) or evidence.get("dub_relation")
    title_ok = evidence.get("title_similarity", 0) >= 0.85
    if exact_id and (title_ok or tags_full):
        return True
    if dub_relation and tags_full and count_equal and (title_ok or evidence.get("t_book_tail_equal")):
        return True
    return bool(evidence.get("manual_verification"))
