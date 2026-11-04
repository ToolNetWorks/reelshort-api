"""Unit tests for edition-match scoring/classification."""

from __future__ import annotations

from app.crawler.scoring import (
    EditionFacts,
    classify,
    score_candidate,
    tag_jaccard,
    t_book_tail,
    title_similarity,
)


def test_t_book_tail() -> None:
    assert t_book_tail("204200150000005664") == t_book_tail("244201150000005664")
    assert t_book_tail("123") is None


def test_tag_jaccard() -> None:
    assert tag_jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert tag_jaccard(["a", "b"], ["b", "c"]) == 1 / 3
    assert tag_jaccard([], ["a"]) is None


def test_same_locale_is_never_a_match() -> None:
    source = EditionFacts(book_id="A", lang="en", book_title="Genie")
    target = EditionFacts(book_id="B", lang="en", book_title="Genie")
    score, evidence = score_candidate(source, target)
    assert score == 0.0
    assert evidence["rejected"] == "same_locale"


def sample_facts(**overrides: object) -> EditionFacts:
    base = dict(
        book_id="6a5191d8fa9f9f7a09081789",
        lang="en",
        book_title="The Great and Powerful Genie",
        t_book_id="204200150000005664",
        chapter_count=51,
        tag_ids=[f"tag{i}" for i in range(26)],
        book_genre=15,
        book_source=45,
        is_dub=False,
        publish_at=1784012406,
    )
    base.update(overrides)
    return EditionFacts(**base)  # type: ignore[arg-type]


def test_strong_match_is_verified() -> None:
    source = sample_facts(
        known_titles=["The Great and Powerful Genie", "Thần Đèn Vĩ Đại và Toàn Năng"],
    )
    target = sample_facts(
        book_id="6a6da2cc39cbfeadc50d1e6a",
        lang="vi",
        book_title="Thần Đèn Vĩ Đại và Toàn Năng",
        t_book_id="244201150000005664",
        is_dub=True,
        original_lang="en",
        publish_at=1786349706,
    )
    score, evidence = score_candidate(source, target)
    assert score >= 0.8, evidence
    assert evidence["t_book_tail_equal"]
    assert evidence["tag_ids_jaccard"] == 1.0
    assert evidence["title_similarity"] == 1.0
    status = classify(score, evidence, min_confidence=0.5, auto_verify_confidence=0.8)
    assert status == "VERIFIED"


def test_weak_match_stays_candidate_or_unknown() -> None:
    source = sample_facts()
    target = sample_facts(
        book_id="ffffffffffffffffffffffff",
        lang="vi",
        book_title="Something Else Entirely",
        t_book_id="999900000000000001",
        chapter_count=30,
        tag_ids=["other1", "other2"],
        book_genre=3,
        book_source=1,
        publish_at=0,
        known_titles=["Unrelated"],
    )
    score, evidence = score_candidate(source, target)
    assert score < 0.5
    assert classify(score, evidence) == "UNKNOWN"


def test_exact_t_book_id_match_auto_verifies() -> None:
    source = sample_facts()
    target = sample_facts(book_id="dup-id", lang="es", book_title="El gran y poderoso genio",
                          known_titles=["The Great and Powerful Genie"])
    score, evidence = score_candidate(source, target)
    assert evidence["t_book_id_equal"] is True
    assert classify(score, evidence) == "VERIFIED"


def test_title_similarity_uses_known_titles() -> None:
    # identical titles score 1.0, unrelated titles score low, and a real
    # translation of the same story still scores above unrelated ones.
    unrelated = title_similarity("Zombie CEO Revenge", ["The Great and Powerful Genie"])
    translation = title_similarity("El gran y poderoso genio", ["The Great and Powerful Genie"])
    assert unrelated < 0.4
    assert translation > unrelated
    assert title_similarity("The Great and Powerful Genie", ["The Great and Powerful Genie"]) == 1.0
    assert title_similarity("Thần Đèn Vĩ Đại và Toàn Năng", ["Thần Đèn Vĩ Đại và Toàn Năng"]) == 1.0
