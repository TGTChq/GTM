"""Transport instrumentation: the measuring fetcher, denominators, truncation.

The whole harness rests on one observation: every acquisition lane already
routes its HTTP through an injectable seam.

* ``free_job_sources``, ``adzuna_client`` and ``ats_board_registry`` all accept
  a ``Fetcher`` (``free_job_sources.py:55``).
* ``jsearch_scraper.fetch_jobs_for_role`` now accepts a ``transport`` with the
  exact signature of ``http_utils.request_with_retry``.

So the harness never re-implements retrieval. It wraps the seam, watches what
crosses it, and hands the payload through untouched. ``MeasuringFetcher`` is
observationally transparent by construction: it returns the inner fetcher's
object, unmodified, and the parity tests assert that adapters produce
byte-identical ``SourceResult``s with and without it.

Denominators are read here rather than in the adapters, because the adapters
either discard them (``adzuna_client.py:379`` never reads ``count``;
``ats_board_registry.py:965`` and ``:1133`` compute a total and throw it away)
or expose only the last one seen. Reading them at the transport boundary needs
no adapter change at all.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import config
import requests
from free_job_sources import FetchPayload, SourceResult, default_fetcher

from retrieval_measurement.identity import utc_stamp
from retrieval_measurement.schema import DenominatorRecord, RequestRecord, TruncationRecord

#: provider -> (json field, scope, human-readable semantics).
#:
#: Only fields the provider itself publishes. Nothing here is inferred, and a
#: provider absent from this table honestly has no denominator rather than a
#: guessed one.
DENOMINATOR_FIELDS: Dict[str, Tuple[str, str, str]] = {
    "himalayas": (
        "totalCount",
        "whole_feed",
        "Total remote jobs in the Himalayas feed at request time, across all "
        "titles and countries. NOT a US-only or role-filtered total.",
    ),
    "adzuna": (
        "count",
        "per_query",
        "Adzuna's own total match count for this exact query (what + "
        "max_days_old + country), before our results_per_page slice.",
    ),
    "ats_smartrecruiters": (
        "totalFound",
        "per_board",
        "Total public postings on this SmartRecruiters board, all titles.",
    ),
    "ats_workday": (
        "total",
        "per_board",
        "Total postings matching the Workday board query, all titles.",
    ),
}


@dataclass
class _Context:
    source: str = "unknown"
    query_key: str = ""
    board_key: str = ""


# --------------------------------------------------------------------------
# Global outbound-request ceiling
# --------------------------------------------------------------------------


class RequestCeilingReached(RuntimeError):
    """The run's global outbound-request ceiling would be exceeded.

    Deliberately its own exception type. A ceiling stop is a decision WE made
    and says nothing about the provider, so it must never be reachable through
    the truncation vocabulary -- ``provider_exhaustion``, ``empty_page``,
    ``quota_guard`` and ``error_stop`` all describe the provider or the network,
    and every one of them would misattribute our own budget to them.
    """


class RequestBudget:
    """One global counter for every physical outbound request in a run.

    Counting at the measuring seam would be wrong here. ``default_fetcher``
    follows up to four redirects inside a single seam call
    (``free_job_sources.py:121``), and ``request_with_retry`` retries inside a
    single JSearch transport call (``http_utils.py:62``); both are real packets
    on the wire and both are invisible from above.

    So the budget installs itself at the one point all three lanes physically
    converge -- ``requests.request`` -- reserves BEFORE delegating, and restores
    the original on exit. Nothing in production is modified; the patch lives
    exactly as long as the ``installed()`` block.

    ``limit=None`` disables the ceiling entirely, so behaviour outside a
    controlled run is byte-identical to having no budget at all.
    """

    def __init__(
        self,
        limit: Optional[int] = None,
        *,
        lane_limits: Optional[Mapping[str, int]] = None,
        provider_limits: Optional[Mapping[str, int]] = None,
        board_limit: Optional[int] = None,
        reserved_for_lanes: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.limit = None if limit is None else int(limit)
        self.lane_limits = {str(k): int(v) for k, v in dict(lane_limits or {}).items()}
        self.provider_limits = {str(k): int(v) for k, v in dict(provider_limits or {}).items()}
        self.board_limit = None if board_limit is None else int(board_limit)
        #: Capacity that a named lane is guaranteed. Other lanes may not consume
        #: it. Without this, ATS spends the whole run's budget before JSearch is
        #: reached -- which is exactly what happened in 20260805T015708Z-1ad3ef58.
        self.reserved_for_lanes = {str(k): int(v) for k, v in dict(reserved_for_lanes or {}).items()}
        self.count = 0
        self.lane = ""
        self.source = ""
        self.board = ""
        self.per_lane: Dict[str, int] = {}
        self.per_provider: Dict[str, int] = {}
        self.per_board: Dict[str, int] = {}
        self.stop_reason = ""
        self.exhausted_scopes: List[Dict[str, Any]] = []
        self.blocked_next_request: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        """True only when the RUN budget is gone. A spent board, provider or
        lane budget stops that scope alone and leaves the run able to continue."""
        return self.stop_reason == "request_ceiling_reached"

    def _reserved_elsewhere(self, lane: str) -> int:
        """Capacity promised to OTHER lanes that this lane may not touch."""
        return sum(
            max(0, amount - self.per_lane.get(name, 0))
            for name, amount in self.reserved_for_lanes.items()
            if name != lane
        )

    def _refuse(self, scope: str, name: str, limit: int, url: Any) -> None:
        try:
            hostname = urlparse(str(url or "")).hostname or ""
        except ValueError:
            hostname = ""
        record = {
            "scope": scope,
            "name": name,
            "limit": limit,
            "sequence": self.count + 1,
            "lane": self.lane,
            "source": self.source,
            "board": self.board,
            # Hostname only: a full URL can carry a key in its query string,
            # and this record is written to an artifact.
            "hostname": hostname,
        }
        self.blocked_next_request = record
        if record not in self.exhausted_scopes:
            self.exhausted_scopes.append(record)
        if scope == "run":
            self.stop_reason = "request_ceiling_reached"
        raise RequestCeilingReached(
            f"{scope} request budget of {limit} reached for {name or 'run'}; refusing "
            f"request {self.count + 1} to {hostname or 'unknown host'} "
            f"(lane={self.lane or 'unknown'}). No request was sent."
        )

    def reserve(self, url: Any = "") -> int:
        """Claim one request slot, or refuse before anything leaves the process.

        Scopes are checked innermost-first so the narrowest budget is blamed:
        a board that runs out is a board problem, not a run problem.
        """
        with self._lock:
            board_key = self.board
            if self.board_limit is not None and board_key:
                if self.per_board.get(board_key, 0) + 1 > self.board_limit:
                    self._refuse("board", board_key, self.board_limit, url)
            provider_limit = self.provider_limits.get(self.source)
            if provider_limit is not None:
                if self.per_provider.get(self.source, 0) + 1 > provider_limit:
                    self._refuse("provider", self.source, provider_limit, url)
            lane_limit = self.lane_limits.get(self.lane)
            if lane_limit is not None:
                if self.per_lane.get(self.lane, 0) + 1 > lane_limit:
                    self._refuse("lane", self.lane, lane_limit, url)
            if self.limit is not None:
                available = self.limit - self._reserved_elsewhere(self.lane)
                if self.count + 1 > available:
                    scope = "run" if available >= self.limit else "lane_reservation"
                    self._refuse(scope, self.lane if scope != "run" else "", available, url)

            self.count += 1
            if self.lane:
                self.per_lane[self.lane] = self.per_lane.get(self.lane, 0) + 1
            if self.source:
                self.per_provider[self.source] = self.per_provider.get(self.source, 0) + 1
            if board_key:
                self.per_board[board_key] = self.per_board.get(board_key, 0) + 1
            # Classified at the same instant the slot is spent, so attribution
            # and the budget can never disagree about how many requests there
            # were. No-op unless a trace is installed.
            from retrieval_measurement import request_trace

            request_trace.classify(board=board_key)
            return self.count

    def would_block(
        self,
        *,
        lane: Optional[str] = None,
        source: Optional[str] = None,
        board: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Would the NEXT request be refused? Read-only; consumes nothing.

        Returns the innermost scope that would refuse, exactly as ``reserve``
        would blame it -- ``board``, then ``provider``, then ``lane``, then the
        run (as ``run`` or ``lane_reservation``). Returns ``None`` when one more
        request is affordable in every scope. Nothing is counted, no
        ``exhausted_scopes`` entry is written and no ``stop_reason`` is set: this
        is the pre-flight the caller uses to skip a board *before* the transport,
        so it must have no side effect of its own.
        """
        lane = self.lane if lane is None else lane
        source = self.source if source is None else source
        board = self.board if board is None else board
        with self._lock:
            if self.board_limit is not None and board:
                if self.per_board.get(board, 0) + 1 > self.board_limit:
                    return {"scope": "board", "name": board, "limit": self.board_limit}
            provider_limit = self.provider_limits.get(source)
            if provider_limit is not None:
                if self.per_provider.get(source, 0) + 1 > provider_limit:
                    return {"scope": "provider", "name": source, "limit": provider_limit}
            lane_limit = self.lane_limits.get(lane)
            if lane_limit is not None:
                if self.per_lane.get(lane, 0) + 1 > lane_limit:
                    return {"scope": "lane", "name": lane, "limit": lane_limit}
            if self.limit is not None:
                available = self.limit - self._reserved_elsewhere(lane)
                if self.count + 1 > available:
                    scope = "run" if available >= self.limit else "lane_reservation"
                    return {
                        "scope": scope,
                        "name": lane if scope != "run" else "",
                        "limit": available,
                    }
        return None

    @contextmanager
    def context(
        self, lane: str = "", source: str = "", board: str = ""
    ) -> Iterator["RequestBudget"]:
        previous = (self.lane, self.source, self.board)
        self.lane = lane or self.lane
        self.source = source or self.source
        self.board = board or self.board
        try:
            yield self
        finally:
            self.lane, self.source, self.board = previous

    @contextmanager
    def installed(self) -> Iterator["RequestBudget"]:
        if self.limit is None:
            yield self
            return
        original = requests.request

        def counting(*args: Any, **kwargs: Any):
            url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
            self.reserve(url)
            return original(*args, **kwargs)

        requests.request = counting  # type: ignore[assignment]
        try:
            yield self
        finally:
            requests.request = original  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit": self.limit,
            "enforced": self.limit is not None
            or bool(self.lane_limits or self.provider_limits or self.board_limit),
            "requests_completed": self.count,
            "stop_reason": self.stop_reason,
            "blocked_next_request": self.blocked_next_request,
            "exhausted_scopes": list(self.exhausted_scopes),
            "lane_limits": dict(self.lane_limits),
            "provider_limits": dict(self.provider_limits),
            "board_limit": self.board_limit,
            "reserved_for_lanes": dict(self.reserved_for_lanes),
            "requests_by_lane": dict(self.per_lane),
            "requests_by_provider": dict(self.per_provider),
            "requests_by_board": dict(self.per_board),
        }


class MeasuringFetcher:
    """Wrap a ``Fetcher`` and record what crosses it.

    Transparent: the inner payload object is returned as-is. The wrapper adds
    no retries, no rewriting, no filtering.
    """

    def __init__(self, inner: Optional[Callable[..., FetchPayload]] = None) -> None:
        self.inner = inner or default_fetcher
        self.requests: List[RequestRecord] = []
        self.denominators: List[DenominatorRecord] = []
        self._context = _Context()
        self._sequence = 0

    # -- attribution -------------------------------------------------------

    @contextmanager
    def context(self, source: str, query_key: str = "", board_key: str = "") -> Iterator["MeasuringFetcher"]:
        """Attribute every request made inside the block to one source/scope.

        Without this, per-source attribution would have to be reverse-engineered
        from URLs -- which is precisely the guesswork that makes the current
        pipeline's global-only counters unusable.
        """
        previous = self._context
        self._context = _Context(source=source, query_key=query_key, board_key=board_key)
        try:
            yield self
        finally:
            self._context = previous

    # -- the seam ----------------------------------------------------------

    def __call__(self, url: str, **kwargs: Any) -> FetchPayload:
        ctx = self._context
        started = time.perf_counter()
        payload = self.inner(url, **kwargs)
        duration = round(time.perf_counter() - started, 4)

        self._sequence += 1
        params = kwargs.get("params") or {}
        text = getattr(payload, "text", "") or ""
        self.requests.append(
            RequestRecord(
                sequence=self._sequence,
                source=ctx.source,
                url=str(getattr(payload, "url", url) or url),
                method=str(kwargs.get("method", "GET")),
                # Keys only. Adzuna params carry app_id/app_key, and a
                # measurement artifact is not a place for credentials.
                param_keys=sorted(str(key) for key in dict(params).keys()),
                query_key=ctx.query_key,
                board_key=ctx.board_key,
                status_code=getattr(payload, "status_code", None),
                response_bytes=len(text.encode("utf-8", "ignore")),
                duration_seconds=duration,
                error=str(getattr(payload, "error", "") or ""),
            )
        )
        self._observe_denominator(ctx, text)
        return payload

    def _observe_denominator(self, ctx: _Context, text: str) -> None:
        spec = DENOMINATOR_FIELDS.get(ctx.source)
        if not spec or not text:
            return
        field_name, scope, semantics = spec
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        raw = data.get(field_name)
        if raw is None and ctx.source == "ats_smartrecruiters":
            raw = data.get("total")  # ats_board_registry.py:965 accepts either
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        self.denominators.append(
            DenominatorRecord(
                provider=ctx.source,
                value=value,
                field_name=field_name,
                scope=scope,
                scope_key=ctx.board_key or ctx.query_key or "",
                semantics=semantics,
                observed_at=utc_stamp(),
            )
        )

    # -- reporting ---------------------------------------------------------

    def requests_for(self, source: str) -> List[RequestRecord]:
        return [record for record in self.requests if record.source == source]

    def denominator_for(self, source: str) -> Optional[DenominatorRecord]:
        """The defensible single denominator for a source, or None.

        Per-query and per-board denominators are summed ONLY across distinct
        scope keys within the same provider. Denominators from different
        providers are never summed: they count overlapping, differently-defined
        populations, and adding them would manufacture a total that does not
        exist.
        """
        records = [record for record in self.denominators if record.provider == source]
        if not records:
            return None
        first = records[0]
        if first.scope == "whole_feed":
            # Repeated observations of the same feed total: take the maximum
            # rather than the sum.
            best = max(records, key=lambda record: record.value)
            return best
        by_scope: Dict[str, int] = {}
        for record in records:
            key = record.scope_key
            by_scope[key] = max(by_scope.get(key, 0), record.value)
        return DenominatorRecord(
            provider=source,
            value=sum(by_scope.values()),
            field_name=first.field_name,
            scope=first.scope,
            scope_key=f"sum_of_{len(by_scope)}_scopes",
            semantics=first.semantics + (
                f" Summed across {len(by_scope)} distinct scopes of the same "
                "provider; scopes may overlap, so this is an upper bound."
            ),
            observed_at=utc_stamp(),
        )


# --------------------------------------------------------------------------
# JSearch transport
# --------------------------------------------------------------------------


class _ReplayResponse:
    """Minimal stand-in for ``requests.Response``.

    ``jsearch_scraper`` touches ``.json()``, ``.headers``, ``.url``, ``.text``
    and (only on a parse failure, via ``http_utils.safe_json``)
    ``.request.method``. Nothing else is needed.
    """

    class _Request:
        method = "GET"

    def __init__(self, payload: Mapping[str, Any], url: str = "", headers: Optional[Mapping[str, str]] = None) -> None:
        self._payload = dict(payload)
        self.url = url
        self.headers: Dict[str, str] = dict(headers or {})
        self.status_code = 200
        self.request = self._Request()

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> Dict[str, Any]:
        return dict(self._payload)


class JSearchTransport:
    """Recording transport for ``jsearch_scraper.fetch_jobs_for_role``.

    Signature-compatible with ``http_utils.request_with_retry``. In replay mode
    it serves recorded payloads keyed by the query string that the *production*
    query builder produced -- so query construction, pagination and stop
    reasons stay entirely in production code and cannot silently diverge.
    """

    def __init__(
        self,
        recorded: Optional[Mapping[str, Any]] = None,
        inner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.recorded = dict(recorded or {})
        self.inner = inner
        self.requests: List[RequestRecord] = []
        self.misses: List[str] = []
        self._sequence = 0

    @staticmethod
    def replay_key(params: Mapping[str, Any]) -> str:
        return f"{params.get('query', '')}|page={params.get('page', '')}"

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        params = dict(kwargs.get("params") or {})
        key = self.replay_key(params)
        started = time.perf_counter()

        if self.inner is not None:
            response = self.inner(method, url, **kwargs)
        else:
            payload = self.recorded.get(key)
            if payload is None:
                self.misses.append(key)
                payload = {"status": "OK", "data": []}
            response = _ReplayResponse(payload, url=url)

        duration = round(time.perf_counter() - started, 4)
        self._sequence += 1
        text = getattr(response, "text", "") or ""
        self.requests.append(
            RequestRecord(
                sequence=self._sequence,
                source="jsearch",
                url=url,
                method=method,
                param_keys=sorted(str(name) for name in params),
                query_key=key,
                status_code=getattr(response, "status_code", None),
                response_bytes=len(str(text).encode("utf-8", "ignore")),
                duration_seconds=duration,
            )
        )
        return response


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------


#: JSearch returns at most this many postings per page
#: (``jsearch_scraper.py:_JSEARCH_FULL_PAGE_SIZE``). A query that comes back
#: with exactly pages x 10 hit our page ceiling; one that comes back short
#: exhausted the provider's supply for that query.
_JSEARCH_PAGE_SIZE = 10


def classify_jsearch_truncation(
    result: SourceResult,
    *,
    scope_key: str = "",
) -> List[TruncationRecord]:
    """Name the condition that actually stopped JSearch, per query and lane.

    The scraper already records every stop condition it evaluated; the harness
    only has to read the right one instead of reaching for a free-feed cap.
    Nothing about retrieval depth, query planning, pagination or quota is
    changed here -- this reads stats that already exist.
    """
    stats = dict((result.metadata or {}).get("stats") or {})
    per_query = dict(stats.get("raw_role_counts") or {})
    pages = max(1, int(stats.get("num_pages_per_query") or config.NUM_PAGES or 1))
    page_ceiling = pages * _JSEARCH_PAGE_SIZE
    records: List[TruncationRecord] = []

    def add(kind: str, reason: str, *, cap=None, evidence=None, key=""):
        records.append(TruncationRecord(
            source="jsearch",
            scope_key=key or scope_key,
            kind=kind,
            detected=True,
            reason=reason,
            applied_cap=cap,
            evidence=dict(evidence or {}),
        ))

    # -- lane-level stop conditions, in the order the scraper evaluates them --
    quota = dict(stats.get("quota") or {})
    remaining = quota.get("remaining")
    threshold = int(config.JSEARCH_MIN_REMAINING_REQUESTS)
    if (
        config.JSEARCH_STOP_ON_LOW_QUOTA
        and threshold > 0
        and isinstance(remaining, int)
        and remaining <= threshold
    ):
        add("quota_guard", "JSEARCH_MIN_REMAINING_REQUESTS reached", cap=threshold,
            evidence={"remaining": remaining, "threshold": threshold})

    budget = int(stats.get("estimated_unit_budget") or 0)
    used = int(stats.get("estimated_request_units") or 0)
    if budget > 0 and used >= budget:
        add("configured_cap", "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN reached", cap=budget,
            evidence={"estimated_request_units": used, "budget": budget})

    adaptive_stop = str(stats.get("adaptive_stop_reason") or "")
    if adaptive_stop:
        extra = int(stats.get("adaptive_extra_queries") or 0)
        cap = int(stats.get("adaptive_query_cap") or config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES)
        add("configured_cap", f"adaptive deepening stopped: {adaptive_stop}", cap=cap,
            evidence={"adaptive_extra_queries": extra, "cap": cap})
    lookback_used = int(stats.get("adaptive_lookback_queries") or 0)
    lookback_cap = int(config.JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES)
    if lookback_used and lookback_cap and lookback_used >= lookback_cap:
        add("configured_cap", "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES reached",
            cap=lookback_cap, evidence={"lookback_queries": lookback_used})

    if result.errors:
        add("error_stop", str(result.errors[0])[:200],
            evidence={"error_count": len(result.errors)})

    # -- per-query attribution: the page ceiling versus genuine exhaustion ----
    saturated = sorted(q for q, n in per_query.items() if int(n) >= page_ceiling)
    exhausted = sorted(q for q, n in per_query.items() if 0 < int(n) < page_ceiling)
    empty = sorted(q for q, n in per_query.items() if int(n) == 0)

    if saturated:
        add("configured_cap",
            f"NUM_PAGES page ceiling reached on {len(saturated)} of {len(per_query)} queries",
            cap=page_ceiling,
            evidence={
                "queries_at_page_ceiling": len(saturated),
                "queries_total": len(per_query),
                "records_per_query_ceiling": page_ceiling,
                "num_pages_per_query": pages,
                "examples": saturated[:5],
            })
    if exhausted:
        add("provider_exhaustion",
            f"{len(exhausted)} quer(y|ies) returned fewer than the page ceiling",
            evidence={"queries_exhausted": len(exhausted), "examples": exhausted[:5]})
    if empty:
        add("empty_page", f"{len(empty)} quer(y|ies) returned nothing",
            evidence={"queries_empty": len(empty), "examples": empty[:5]})

    if not records:
        add("not_truncated", "no JSearch stop condition was detected")
    return records


def classify_truncation(
    source: str,
    result: SourceResult,
    *,
    denominator: Optional[DenominatorRecord] = None,
    scope_key: str = "",
) -> List[TruncationRecord]:
    """Decide, per source, whether we stopped by choice or ran out of supply.

    Exact-cap signatures matter: ``len(jobs) == cap`` is a configured ceiling,
    not a coincidence, and reporting it as "that is all there was" is how a
    retrieval limit hides for months.
    """
    records: List[TruncationRecord] = []
    collected = len(result.jobs)
    record_cap = max(1, int(config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE))

    if source == "jsearch":
        # FREE_SOURCE_MAX_RECORDS_PER_SOURCE does not govern this lane. Run
        # 20260805T021929Z-b25aaad1 reported it as JSearch's controlling cap
        # with applied_cap=1000 against collected=1340 -- self-evidently
        # inapplicable, and it hid the real cause, which the per-query
        # distribution shows plainly: 47 of 50 queries returned exactly 30
        # records, the NUM_PAGES=3 x 10-per-page ceiling.
        return classify_jsearch_truncation(result, scope_key=scope_key)

    if collected >= record_cap:
        records.append(TruncationRecord(
            source=source,
            scope_key=scope_key,
            kind="configured_cap",
            detected=True,
            reason="FREE_SOURCE_MAX_RECORDS_PER_SOURCE reached",
            applied_cap=record_cap,
            evidence={"collected": collected, "cap": record_cap},
        ))

    if source == "himalayas":
        page_cap = max(1, int(config.HIMALAYAS_MAX_PAGES))
        if result.pages >= page_cap:
            records.append(TruncationRecord(
                source=source,
                scope_key=scope_key,
                kind="configured_cap",
                detected=True,
                reason="HIMALAYAS_MAX_PAGES reached",
                applied_cap=page_cap,
                evidence={"pages": result.pages, "cap": page_cap},
            ))

    if result.errors:
        records.append(TruncationRecord(
            source=source,
            scope_key=scope_key,
            kind="error_stop",
            detected=True,
            reason=result.errors[0][:200],
            evidence={"error_count": len(result.errors)},
        ))

    if denominator is not None:
        unfetched = max(0, denominator.value - collected)
        if unfetched > 0:
            # A shortfall with no cap and no error is NOT provider exhaustion --
            # the provider says there is more and we did not ask for it. Naming
            # that "exhaustion" is precisely how a retrieval ceiling gets
            # explained away, so it gets its own kind and stays conspicuous.
            records.append(TruncationRecord(
                source=source,
                scope_key=denominator.scope_key or scope_key,
                kind="configured_cap" if records else "unexplained_shortfall",
                detected=True,
                reason=(
                    "provider reports more records than we retrieved"
                    if records
                    else "retrieved fewer than the provider total with no cap or error to explain it"
                ),
                known_unfetched=unfetched,
                evidence={
                    "provider_total": denominator.value,
                    "provider_total_field": denominator.field_name,
                    "retrieved": collected,
                    "scope": denominator.scope,
                },
            ))
        else:
            records.append(TruncationRecord(
                source=source,
                scope_key=denominator.scope_key or scope_key,
                kind="provider_exhaustion",
                detected=False,
                reason="retrieved at least the provider-reported total",
                known_unfetched=0,
                evidence={"provider_total": denominator.value, "retrieved": collected},
            ))

    if not records:
        records.append(TruncationRecord(
            source=source,
            scope_key=scope_key,
            kind="not_truncated",
            detected=False,
            reason="no cap, error, or provider total indicated a ceiling",
            evidence={"collected": collected, "pages": result.pages},
        ))
    return records
