"""Multi-language edition discovery.

Strategy (all data comes from the official public site, nothing is invented):

1. ``search``  — query the official per-locale search with every title we know
   for the story (source title plus titles of already-known editions). The
   ReelShort search index carries both localised and original titles, so a
   title hint in any known language can surface another locale's edition.
2. ``related`` — walk the official catalogue listing (``getTagBook``) for the
   target locale, applying a cheap pre-filter before scoring. Bounded by
   ``discovery_tag_scan_pages`` (the listing is not exhaustive upstream, so this
   is best-effort and used only as a secondary source).
3. Scoring  — every candidate is scored against the source edition using
   ``app.crawler.scoring`` (tag-id Jaccard, episode count, official dub
   relation, t_book_id equality/tail, title similarity, genre/source, publish
   proximity). Candidates become CANDIDATE/UNKNOWN; deterministic evidence can
   auto-promote to VERIFIED, otherwise an admin verifies manually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.crawler import store
from app.crawler.parser import ParsedMovie, SearchCandidate, clean_text, normalize_title
from app.crawler.scoring import (
    STATUS_UNKNOWN,
    EditionFacts,
    classify,
    score_candidate,
    t_book_tail,
    title_similarity,
)
from app.crawler.source import ReelShortSource, SourceError, SourceNotFound, SourceUnavailable
from app.db.models import EditionMatch, MovieEdition, utcnow

log = logging.getLogger(__name__)


@dataclass
class DiscoveryOutcome:
    source_book_id: str
    group_id: str | None
    created_matches: int = 0
    updated_matches: int = 0
    locale_results: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    errors: list[str] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_book_id": self.source_book_id,
            "group_id": self.group_id,
            "created_matches": self.created_matches,
            "updated_matches": self.updated_matches,
            "locales": self.locale_results or {},
            "errors": self.errors or [],
        }


class EditionDiscovery:
    def __init__(self, source: ReelShortSource, settings: Settings | None = None) -> None:
        self.source = source
        self.settings = settings or get_settings()

    @staticmethod
    def _checkpoint(
        session: Session, job: Any, checkpoint: dict[str, Any], *, progress: float | None = None
    ) -> None:
        """Record progress on the job row and commit it (resume-safe)."""
        store.save_checkpoint(session, job, checkpoint, progress=progress)

    # --- public API --------------------------------------------------------
    async def discover(
        self,
        session: Session,
        *,
        source_book_id: str,
        targets: Iterable[str] | None = None,
        title_hints: Iterable[str] | None = None,
        strategies: Iterable[str] = ("search",),
        job: Any | None = None,
    ) -> DiscoveryOutcome:
        source_edition = session.get(MovieEdition, source_book_id)
        outcome = DiscoveryOutcome(
            source_book_id=source_book_id,
            group_id=source_edition.movie_group_id if source_edition else None,
            locale_results={},
            errors=[],
        )
        if source_edition is None:
            outcome.errors.append(f"source edition {source_book_id} is not indexed")
            return outcome

        group_titles = self._known_titles(session, source_edition, title_hints)
        target_locales = self._target_locales(source_edition, targets)
        source_facts = self._facts_from_edition(source_edition, known_titles=group_titles)

        total_locales = max(1, len(target_locales))
        for index, locale in enumerate(target_locales):
            if job is not None:
                self._checkpoint(
                    session,
                    job,
                    {
                        "phase": "discovery",
                        "locale": locale,
                        "index": index,
                        "total": total_locales,
                        "titles": group_titles[:5],
                    },
                    progress=round((index / total_locales) * 100, 1),
                )
            try:
                candidates: list[SearchCandidate] = []
                if "search" in strategies:
                    candidates.extend(await self._search_locale(session, locale, group_titles))
                if "related" in strategies:
                    candidates.extend(await self._catalog_locale(session, locale, source_facts))
                result = await self._score_and_link(
                    session, source_edition, source_facts, locale, candidates, group_titles, job=job
                )
                outcome.created_matches += result["created"]
                outcome.updated_matches += result["updated"]
                outcome.locale_results[locale] = result
            except SourceUnavailable as exc:
                outcome.errors.append(f"{locale}: source unavailable ({exc})")
                outcome.locale_results[locale] = {"status": "deferred", "reason": str(exc)}
                log.warning("discovery deferred", extra={"locale": locale, "error": str(exc)})
            except SourceError as exc:
                outcome.errors.append(f"{locale}: {exc}")
                outcome.locale_results[locale] = {"status": "error", "reason": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive
                outcome.errors.append(f"{locale}: {type(exc).__name__}: {exc}")
                log.exception("discovery locale failed", extra={"locale": locale})

        if job is not None:
            job.progress = 100.0
        edition = session.get(MovieEdition, source_book_id)
        if edition is not None:
            outcome.group_id = edition.movie_group_id
            if outcome.group_id:
                store.refresh_group_stats(session, outcome.group_id)
        return outcome

    # --- strategies --------------------------------------------------------
    async def _search_locale(
        self, session: Session, locale: str, titles: list[str]
    ) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        for title in titles[:3]:
            for page in range(1, max(1, self.settings.discovery_search_pages) + 1):
                cache_key = f"search:{locale}:{normalize_title(title)}:{page}"
                cached = store.cache_get(session, cache_key, touch=False)
                if cached is not None:
                    payload = cached
                else:
                    response = await self.source.search_page(locale, title, page=page)
                    from app.crawler.parser import parse_search_page

                    parsed = parse_search_page(response.text, lang_hint=locale)
                    payload = {
                        "url": response.url,
                        "status": response.status,
                        "count": len(parsed),
                        "results": [
                            {
                                "book_id": c.book_id,
                                "book_title": c.book_title,
                                "lang": c.lang,
                                "t_book_id": c.t_book_id,
                                "s_book_id": c.s_book_id,
                                "book_pic": c.book_pic,
                                "chapter_count": c.chapter_count,
                                "is_dub": c.is_dub,
                            }
                            for c in parsed
                        ],
                    }
                    store.cache_set(
                        session,
                        cache_key,
                        "search",
                        payload,
                        timedelta(minutes=self.settings.search_cache_ttl_minutes),
                    )
                    session.flush()
                for item in payload.get("results", []):
                    # search results are locale-scoped; ignore cross-locale noise
                    if item.get("lang") and item.get("lang") != locale:
                        continue
                    candidates.append(
                        SearchCandidate(
                            book_id=str(item["book_id"]),
                            book_title=item["book_title"],
                            lang=item.get("lang") or locale,
                            t_book_id=item.get("t_book_id"),
                            s_book_id=item.get("s_book_id"),
                            book_pic=item.get("book_pic"),
                            chapter_count=item.get("chapter_count"),
                            is_dub=item.get("is_dub"),
                            source="search",
                            extra={"keyword": title, "page": page, "locale": locale},
                        )
                    )
        return candidates

    async def _catalog_locale(
        self, session: Session, locale: str, source_facts: EditionFacts
    ) -> list[SearchCandidate]:
        """Best-effort catalogue walk (bounded). Used as a secondary strategy."""
        from app.crawler.parser import parse_tag_catalog

        out: list[SearchCandidate] = []
        page_size = max(10, min(self.settings.discovery_tag_scan_page_size, 100))
        for page in range(1, max(1, self.settings.discovery_tag_scan_pages) + 1):
            cache_key = f"catalog:{locale}:{page}:{page_size}"
            cached = store.cache_get(session, cache_key, touch=False)
            if cached is None:
                payload = await self.source.tag_catalog(
                    self.settings.catalog_tag_id, locale, page=page, page_size=page_size
                )
                books, total = parse_tag_catalog(payload, locale)
                cached = {
                    "total_items": total,
                    "results": [
                        {
                            "book_id": b.book_id,
                            "book_title": b.book_title,
                            "t_book_id": b.t_book_id,
                            "chapter_count": b.chapter_count,
                            "book_genre": b.extra.get("book_genre"),
                            "book_source": b.extra.get("book_source"),
                            "tag_names": b.extra.get("tag_names"),
                        }
                        for b in books
                    ],
                }
                store.cache_set(
                    session,
                    cache_key,
                    "catalog",
                    cached,
                    timedelta(hours=self.settings.edition_ttl_hours),
                )
                session.flush()
            for item in cached.get("results", []):
                # cheap pre-filter: episode count must be plausible
                if (
                    source_facts.chapter_count
                    and item.get("chapter_count")
                    and abs(int(item["chapter_count"]) - source_facts.chapter_count) > 2
                ):
                    continue
                out.append(
                    SearchCandidate(
                        book_id=str(item["book_id"]),
                        book_title=item["book_title"],
                        lang=locale,
                        t_book_id=item.get("t_book_id"),
                        chapter_count=item.get("chapter_count"),
                        source="catalog",
                        extra=item,
                    )
                )
        return out

    # --- scoring / linking -------------------------------------------------
    async def _score_and_link(
        self,
        session: Session,
        source_edition: MovieEdition,
        source_facts: EditionFacts,
        locale: str,
        candidates: list[SearchCandidate],
        group_titles: list[str],
        job: Any | None = None,
    ) -> dict[str, Any]:
        deduped: dict[str, SearchCandidate] = {}
        for candidate in candidates:
            if candidate.book_id == source_edition.book_id:
                continue
            if candidate.book_id in deduped:
                continue
            deduped[candidate.book_id] = candidate
        budget = max(0, self.settings.discovery_max_candidates)
        floor = self.settings.discovery_candidate_min_score
        # Only spend upstream requests on candidates that already carry a
        # plausible identity signal (t_book_id/tail, official dub relation or a
        # strong title match); the catalogue scan returns many unrelated books.
        preselected = [
            (candidate, self._pre_score(source_facts, candidate))
            for candidate in deduped.values()
            if self._pre_filter(source_facts, candidate)
        ]
        preselected = [item for item in preselected if item[1] >= floor]
        selected = [
            candidate
            for candidate, _score in sorted(preselected, key=lambda item: item[1], reverse=True)[:budget]
        ]

        created = updated = 0
        scored: list[dict[str, Any]] = []
        for position, candidate in enumerate(selected):
            if job is not None:
                self._checkpoint(
                    session,
                    job,
                    {
                        "phase": "scoring",
                        "locale": locale,
                        "candidate": candidate.book_id,
                        "index": position,
                        "total": len(selected),
                    },
                )
            try:
                facts, edition = await self._candidate_facts(session, candidate, locale)
            except SourceUnavailable:
                raise
            except (SourceError, SourceNotFound) as exc:
                log.debug("candidate fetch failed", extra={"book_id": candidate.book_id, "error": str(exc)})
                continue
            confidence, evidence = score_candidate(source_facts, facts)
            status = classify(
                confidence,
                evidence,
                min_confidence=self.settings.discovery_min_confidence,
                auto_verify_confidence=self.settings.discovery_auto_verify_confidence,
            )
            evidence["method"] = candidate.source
            evidence["locale"] = locale
            if candidate.extra.get("keyword"):
                # official search returned this book for one of our known titles
                evidence["found_by_search_keyword"] = candidate.extra["keyword"]
            if status != STATUS_UNKNOWN:
                created_delta, updated_delta = self._upsert_match(
                    session,
                    source_edition=source_edition,
                    edition=edition,
                    confidence=confidence,
                    status=status,
                    method=candidate.source,
                    evidence=evidence,
                )
                created += created_delta
                updated += updated_delta
            scored.append(
                {
                    "book_id": candidate.book_id,
                    "lang": edition.lang,
                    "title": edition.book_title,
                    "confidence": confidence,
                    "status": status,
                    "method": candidate.source,
                }
            )
        scored.sort(key=lambda item: item["confidence"], reverse=True)
        return {
            "status": "ok",
            "candidates_considered": len(selected),
            "matches": scored[:20],
            "created": created,
            "updated": updated,
        }

    def _pre_filter(self, source: EditionFacts, candidate: SearchCandidate) -> bool:
        """Cheap identity gate deciding whether a candidate is worth fetching.

        Signals available before spending a request (all from the official
        listing/search payload): exact ``t_book_id``, matching ``t_book_id``
        tail (story-level id shared by editions), the official dub flag with an
        equal episode count, or a strong title match against a known title.
        """
        if source.t_book_id and candidate.t_book_id:
            if source.t_book_id == candidate.t_book_id:
                return True
            source_tail, candidate_tail = t_book_tail(source.t_book_id), t_book_tail(candidate.t_book_id)
            if source_tail and source_tail == candidate_tail:
                return True
        if candidate.is_dub and source.chapter_count and candidate.chapter_count == source.chapter_count:
            return True
        similarity = title_similarity(candidate.book_title, source.known_titles or [source.book_title])
        return similarity >= self.settings.discovery_candidate_min_title_similarity

    def _pre_score(self, source: EditionFacts, candidate: SearchCandidate) -> float:
        score = 0.0
        if source.t_book_id and candidate.t_book_id and source.t_book_id == candidate.t_book_id:
            score += 1.0
        if source.t_book_id and candidate.t_book_id and t_book_tail(source.t_book_id) == t_book_tail(
            candidate.t_book_id
        ):
            score += 0.4
        if source.chapter_count and candidate.chapter_count == source.chapter_count:
            score += 0.5
        score += title_similarity(candidate.book_title, source.known_titles or [source.book_title])
        if candidate.source == "search":
            score += 0.2
        if candidate.is_dub is not None and candidate.is_dub:
            score += 0.1
        return score

    async def _candidate_facts(
        self, session: Session, candidate: SearchCandidate, locale: str
    ) -> tuple[EditionFacts, MovieEdition]:
        edition = session.get(MovieEdition, candidate.book_id)
        if edition is None:
            payload = await self.source.movie_info(candidate.book_id, lang=locale)
            from app.crawler.parser import parse_movie_payload

            parsed: ParsedMovie = parse_movie_payload(
                payload, source_url=f"{self.source.base_url}/movie/{candidate.book_id}"
            )
            edition = store.upsert_edition(session, parsed, source_kind="discovery")
            if edition.movie_group_id is None:
                # every edition belongs to a group (singleton until matched)
                store.ensure_group(session, edition)
            if parsed.episodes:
                store.upsert_episodes(session, parsed.book_id, parsed.lang, parsed.episodes)
            store.record_history(
                session,
                outcome="discovery_candidate",
                book_id=parsed.book_id,
                lang=parsed.lang,
                url=parsed.source_url,
                source_kind="api",
                http_status=200,
                items_written=len(parsed.episodes),
            )
            session.flush()
        facts = self._facts_from_edition(edition, known_titles=[candidate.book_title])
        return facts, edition

    def _upsert_match(
        self,
        session: Session,
        *,
        source_edition: MovieEdition,
        edition: MovieEdition,
        confidence: float,
        status: str,
        method: str,
        evidence: dict[str, Any],
    ) -> tuple[int, int]:
        stmt = select(EditionMatch).where(
            EditionMatch.source_book_id == source_edition.book_id,
            EditionMatch.target_book_id == edition.book_id,
        )
        match = session.scalar(stmt)
        created = updated = 0
        if match is None:
            match = EditionMatch(
                source_book_id=source_edition.book_id,
                target_book_id=edition.book_id,
                source_lang=source_edition.lang,
                target_lang=edition.lang,
                status=status,
                confidence=confidence,
                method=method,
                evidence=evidence,
            )
            session.add(match)
            created = 1
        else:
            if match.status == "REJECTED":
                return 0, 0
            if match.status != "VERIFIED" or status == "VERIFIED":
                match.confidence = max(match.confidence or 0.0, confidence)
                match.status = status if match.status != "VERIFIED" else "VERIFIED"
            match.evidence = {**(match.evidence or {}), **evidence}
            match.method = method
            updated = 1
        try:
            session.flush()
        except IntegrityError:  # pragma: no cover - concurrent discovery
            session.rollback()
            return 0, 0
        if status in ("VERIFIED", "CANDIDATE"):
            store.link_group(session, source_edition.book_id, edition.book_id)
            session.refresh(match)
            match.movie_group_id = session.get(MovieEdition, source_edition.book_id).movie_group_id
            session.flush()
        return created, updated

    # --- helpers -----------------------------------------------------------
    def _known_titles(
        self, session: Session, source_edition: MovieEdition, hints: Iterable[str] | None
    ) -> list[str]:
        titles: list[str] = []
        if hints:
            titles.extend(clean_text(h) or "" for h in hints)
        if source_edition.movie_group_id:
            for edition in session.scalars(
                select(MovieEdition).where(MovieEdition.movie_group_id == source_edition.movie_group_id)
            ):
                titles.append(edition.book_title)
        titles.append(source_edition.book_title)
        seen: set[str] = set()
        cleaned: list[str] = []
        for title in titles:
            if not title:
                continue
            key = normalize_title(title)
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned.append(title)
        return cleaned

    def _target_locales(
        self, source_edition: MovieEdition, targets: Iterable[str] | None
    ) -> list[str]:
        if targets:
            locales = [t.strip() for t in targets if t and t.strip()]
        else:
            locales = list(self.settings.discovery_locales)
        locales = [loc for loc in locales if loc and loc != source_edition.lang]
        # already-known editions are still re-checked (cheap) but last
        limit = max(1, self.settings.discovery_max_locales_per_run)
        return locales[:limit]

    def _facts_from_edition(self, edition: MovieEdition, *, known_titles: list[str]) -> EditionFacts:
        return EditionFacts(
            book_id=edition.book_id,
            lang=edition.lang,
            book_title=edition.book_title,
            t_book_id=edition.t_book_id,
            s_book_id=edition.s_book_id,
            chapter_count=edition.chapter_count or edition.stored_chapter_count or None,
            tag_ids=list(edition.tag_ids or []),
            tag_names=list(edition.tags or []),
            book_genre=edition.book_genre,
            book_source=edition.book_source,
            is_dub=edition.is_dub,
            original_lang=edition.original_lang,
            publish_at=edition.publish_at,
            known_titles=known_titles,
            source=edition.source_kind or "indexed",
        )


def candidate_facts_from_dict(payload: dict[str, Any]) -> EditionFacts:
    return EditionFacts(
        book_id=str(payload["book_id"]),
        lang=payload.get("lang") or "unknown",
        book_title=payload.get("book_title") or "",
        t_book_id=payload.get("t_book_id"),
        s_book_id=payload.get("s_book_id"),
        chapter_count=payload.get("chapter_count"),
        tag_ids=list(payload.get("tag_ids") or []),
        tag_names=list(payload.get("tags") or []),
        book_genre=payload.get("book_genre"),
        book_source=payload.get("book_source"),
        is_dub=payload.get("is_dub"),
        original_lang=payload.get("original_lang"),
        publish_at=payload.get("publish_at"),
    )


def utcnow_iso() -> str:
    return utcnow().isoformat()
