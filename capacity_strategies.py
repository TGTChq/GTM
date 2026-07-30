"""Production strategy runners that let the capacity controller materially
alter execution BEFORE paid contact enrichment (Phase 13 integration
correction).

The controller (capacity_controller.py) is the brain; this module supplies
the *real* pre-contact strategy runners that reuse existing production
functions -- ``job_filter.run_filter`` and
``qualification_pipeline.run_precontact_qualification`` -- to surface
additional, genuinely-searchable canonical companies from already-acquired
inventory at wider age windows, without any Apollo/Hunter call.

Only strategies that production code can actually invoke at the pre-contact
boundary are exposed. Age-window recovery is that clean set: 15-30 (standard)
and 31-60 / 61-90 (extended, gated by EXTENDED_AGE_RECOVERY_ENABLED). Other
ladder strategies (JSearch top-up, Adzuna, base multi-source re-run) either
already ran in the base acquisition or operate post-contact, so they are NOT
represented as available here rather than being faked.

A company is counted as searchable only if a real job of its own yields a
safe employer domain via the same ``get_safe_employer_domain`` the Account
Gate waterfall starts from -- never merely because a domain-bearing record
exists.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import config
from capacity_controller import build_from_config
from company_identity import canonical_company_key
from job_filter import get_safe_employer_domain, run_filter, normalize_text
from qualification_pipeline import run_precontact_qualification


def _load_jobs(path: str) -> List[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(j) for j in payload.get("jobs", []) if isinstance(j, dict)]


def precontact_searchable_keys(jobs, blocked_domains) -> Set[str]:
    """Canonical company keys for jobs that genuinely have a safe employer
    domain -- the pre-contact condition to reach people search. Excludes
    publisher/aggregator/ATS hosts (get_safe_employer_domain already does)."""
    keys: Set[str] = set()
    for job in jobs:
        domain = get_safe_employer_domain(job)[0]
        if not domain:
            continue
        key = canonical_company_key(
            domain=domain,
            normalized_name=normalize_text(
                job.get("canonical_employer_name") or job.get("employer_name") or ""
            ),
            blocked_domains=blocked_domains,
        )
        if key:
            keys.add(key)
    return keys


def build_precontact_strategy_runners(
    *,
    scrape_output_path: str,
    registry,
    blocked_domains,
    already_keys: Set[str],
    extra_jobs_sink: List[dict],
    extended_enabled: bool,
) -> Dict[str, Callable[[], Set[str]]]:
    """Map strategy-name -> runner. Each runner re-filters the ALREADY-ACQUIRED
    raw inventory at a wider age window (real pre-contact function reuse),
    qualifies it, appends the new contact-eligible jobs to ``extra_jobs_sink``,
    and returns the set of NEW canonical searchable companies it added."""
    windows = [("age_recovery_15_30", 15, 30)]
    if extended_enabled:
        windows += [("age_recovery_31_60", 31, 60), ("age_recovery_61_90", 61, 90)]

    runners: Dict[str, Callable[[], Set[str]]] = {}

    def _make(name: str, lo: int, hi: int) -> Callable[[], Set[str]]:
        def run() -> Set[str]:
            filtered = run_filter(
                input_path=scrape_output_path, registry=registry,
                max_age_days=hi, min_age_days=lo,
                output_suffix=f"capacity_{name}", allow_empty=True,
            )
            if filtered.kept_count == 0:
                return set()
            qualified = run_precontact_qualification(
                filtered.output_path, suffix=f"capacity_{name}"
            )
            jobs = _load_jobs(qualified.output_path)
            if not jobs:
                return set()
            batch_keys = precontact_searchable_keys(jobs, blocked_domains)
            new_keys = batch_keys - already_keys
            if not new_keys:
                # No incremental searchable company -> nothing to add (the
                # controller will mark this strategy exhausted).
                return set()
            # Append only jobs belonging to a newly-searchable company, so the
            # pool grows with genuinely incremental opportunities.
            for job in jobs:
                domain = get_safe_employer_domain(job)[0]
                if not domain:
                    continue
                key = canonical_company_key(
                    domain=domain,
                    normalized_name=normalize_text(
                        job.get("canonical_employer_name") or job.get("employer_name") or ""
                    ),
                    blocked_domains=blocked_domains,
                )
                if key in new_keys:
                    extra_jobs_sink.append(job)
            already_keys.update(new_keys)
            return new_keys
        return run

    for name, lo, hi in windows:
        runners[name] = _make(name, lo, hi)
    return runners


def expand_precontact_capacity(
    *,
    config_module=config,
    scrape_output_path: str,
    registry,
    current_jobs: List[dict],
    runtime_deadline: Optional[float] = None,
    strategy_runners: Optional[Dict[str, Callable[[], Set[str]]]] = None,
):
    """Run the pre-contact capacity expansion. Returns
    ``(controller_state, extra_contact_eligible_jobs)``.

    When the controller is disabled this is a strict no-op (empty extra jobs),
    guaranteeing baseline-identical behavior. When enabled and below target it
    invokes the real strategy runners until the target/headroom is met, a
    strategy is exhausted, or the runtime guard fires -- always with an exact
    stop reason.
    """
    controller = build_from_config(config_module)
    blocked = config_module.INTERMEDIARY_JOB_DOMAINS
    seed = precontact_searchable_keys(current_jobs, blocked)
    controller.register_searchable(seed)

    if not controller.enabled:
        controller.stop_reason = "controller_disabled"
        return controller.state(), []

    extra_jobs: List[dict] = []
    already: Set[str] = set(seed)
    runners = strategy_runners or build_precontact_strategy_runners(
        scrape_output_path=scrape_output_path, registry=registry,
        blocked_domains=blocked, already_keys=already, extra_jobs_sink=extra_jobs,
        extended_enabled=bool(getattr(config_module, "EXTENDED_AGE_RECOVERY_ENABLED", False)),
    )

    def guard() -> Optional[str]:
        if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
            return "runtime_guard_reached"
        return None

    state = controller.run_until_target(runners, guard=guard)
    state["extra_contact_eligible_jobs"] = len(extra_jobs)
    return state, extra_jobs
