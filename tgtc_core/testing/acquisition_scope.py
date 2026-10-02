"""Offline approximation of documented provider filters, NOT a live API proof.

Only fields with documented semantics are modeled. Never used to approve leads.
Input is provider-shaped (not the core database schema).
"""
import csv


def values(value):
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(v) for v in value}
    return set(next(csv.reader([str(value)]))) - {""}


def matches_filters(row: dict, params: dict) -> bool:
    if params.get("organization_agency") == "exclude" and row.get("org_linkedin_recruitment_agency_derived") is True:
        return False
    for param, field in (("exclude_organization_industry", "org_linkedin_industry"),
                         ("exclude_organization_slug", "org_linkedin_slug")):
        if row.get(field) in values(params.get(param)):
            return False
    for param, field in (("ai_employment_type", "ai_employment_type"), ("ai_taxonomies_a", "ai_taxonomies_a")):
        if params.get(param) and not values(params[param]).intersection(values(row.get(field))):
            return False
    count = row.get("org_linkedin_headcount")
    if "organization_headcount_gte" in params or "organization_headcount_lt" in params:
        if type(count) is not int:
            return False
        if count < int(params.get("organization_headcount_gte", 0)):
            return False
        if "organization_headcount_lt" in params and count >= int(params["organization_headcount_lt"]):
            return False
    return True
