"""Central role taxonomy for TGTC Intent-Based Outbound 2.0.

This module is deliberately dependency-free so config, relevance, routing, and
personalization can all consume the same source of truth without circular
imports. Each role has:

- a canonical title used in Airtable and downstream reporting;
- a function bucket used for campaign routing and company-level grouping;
- a hiring-manager bucket used to choose the most likely functional buyer;
- a safe role-focus fallback for human review and outbound personalization;
- optional title aliases used for relevance matching, not extra API searches.

The catalog reflects Brett's July Week 3 decisions:
- remove broad/non-title AI Training and AI Transformation terms;
- keep Graphic Designer under Marketing;
- omit titles that skew too heavily toward in-person work;
- do not enforce a founded-before-2010 requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple


@dataclass(frozen=True)
class RoleDefinition:
    canonical_title: str
    function_bucket: str
    hiring_manager_bucket: str
    fallback_focus: str
    aliases: Tuple[str, ...] = ()
    context_patterns: Tuple[str, ...] = ()
    negative_patterns: Tuple[str, ...] = ()

    @property
    def match_terms(self) -> Tuple[str, ...]:
        return (self.canonical_title, *self.aliases)


def _role(
    title: str,
    function_bucket: str,
    hiring_manager_bucket: str,
    fallback_focus: str,
    *,
    aliases: Iterable[str] = (),
    context_patterns: Iterable[str] = (),
    negative_patterns: Iterable[str] = (),
) -> RoleDefinition:
    return RoleDefinition(
        canonical_title=title,
        function_bucket=function_bucket,
        hiring_manager_bucket=hiring_manager_bucket,
        fallback_focus=fallback_focus,
        aliases=tuple(aliases),
        context_patterns=tuple(context_patterns),
        negative_patterns=tuple(negative_patterns),
    )


# Titles intentionally omitted from discovery after Brett's review.
REMOVED_ROLE_TITLES: Dict[str, str] = {
    "AI Training": "broad concept rather than a reliable job title",
    "AI Transformation": "broad concept rather than a reliable job title",
    "Help Desk Specialist": "skews heavily toward in-person work",
    "IT Support Specialist": "skews heavily toward in-person work",
    "Logistics Coordinator": "skews heavily toward in-person work",
    "Office Manager": "skews heavily toward in-person work",
    "Supply Chain Analyst": "skews heavily toward in-person work",
    "Procurement Specialist": "skews heavily toward in-person work",
}


_ROLE_DEFINITIONS = [
    # Customer Support & Success
    _role("Bilingual Customer Support Representative", "customer_support", "customer_support", "bilingual customer support, issue resolution, and service operations"),
    _role("Customer Experience Specialist", "customer_support", "customer_support", "customer experience, issue resolution, and service improvement"),
    _role("Customer Onboarding Specialist", "customer_success", "customer_success", "customer onboarding, product adoption, and implementation support"),
    _role("Customer Retention Specialist", "customer_success", "customer_success", "customer retention, renewal support, and churn reduction"),
    _role("Customer Success Associate", "customer_success", "customer_success", "customer onboarding, adoption, and account support"),
    _role("Customer Success Manager", "customer_success", "customer_success", "customer onboarding, retention, and expansion", aliases=("Client Success Manager",)),
    _role("Customer Support", "customer_support", "customer_support", "customer support, issue resolution, and service operations", aliases=("Customer Service",)),
    _role("Customer Support Representative", "customer_support", "customer_support", "customer support, ticket resolution, and service operations"),
    _role("Technical Support Specialist", "customer_support", "customer_support", "technical customer support, troubleshooting, and issue resolution"),
    _role("Implementation Specialist", "customer_success", "customer_success", "customer implementation, onboarding, and product adoption"),
    _role(
        "Community Manager",
        "customer_support",
        "customer_support",
        "community engagement, member support, and customer advocacy",
        negative_patterns=(r"\b(hoa|homeowners association|apartment|residential|property management)\b",),
    ),

    # Engineering, Data & IT
    _role("Backend Developer", "engineering", "engineering", "backend development, APIs, and scalable systems", aliases=("Back-End Developer",)),
    _role("Frontend Developer", "engineering", "engineering", "frontend development, user interfaces, and web performance", aliases=("Front-End Developer",)),
    _role("Full Stack Developer", "engineering", "engineering", "full-stack development, product integrations, and scalable applications", aliases=("Full-Stack Developer",)),
    _role("Software Engineer", "engineering", "engineering", "software development, system design, and production reliability"),
    _role("Cloud Engineer", "engineering", "engineering", "cloud infrastructure, deployment automation, and platform reliability"),
    _role("DevOps Engineer", "engineering", "engineering", "deployment automation, cloud infrastructure, and platform reliability", aliases=("Dev Ops Engineer",)),
    _role("QA Engineer", "engineering", "engineering", "software quality, test automation, and release reliability", aliases=("Quality Assurance Engineer",)),
    _role("QA Analyst", "engineering", "engineering", "software quality, test planning, and defect analysis", aliases=("Quality Assurance Analyst",)),
    _role("Data Analyst", "engineering", "data", "data analysis, reporting, and business insights"),
    _role("Data Engineer", "engineering", "data", "data pipelines, warehouse infrastructure, and data reliability"),
    _role("Data Scientist", "engineering", "data", "data science, predictive modeling, and business insights"),
    _role("Database Administrator", "engineering", "data", "database administration, performance, and data reliability", aliases=("DBA",)),
    _role("Business Intelligence Analyst", "engineering", "data", "business intelligence, dashboards, and decision support", aliases=("BI Analyst",)),
    _role("Systems Administrator", "engineering", "it", "systems administration, infrastructure reliability, and access management", aliases=("System Administrator",)),

    # Finance & Accounting
    _role("Accountant", "finance", "finance", "accounting operations, reconciliations, and financial reporting"),
    _role("Staff Accountant", "finance", "finance", "general ledger accounting, reconciliations, and month-end close"),
    _role("AP Specialist", "finance", "finance", "accounts payable, invoice processing, and vendor reconciliation", aliases=("Accounts Payable Specialist",)),
    _role("AR Specialist", "finance", "finance", "accounts receivable, cash application, and customer collections", aliases=("Accounts Receivable Specialist",)),
    _role("Bookkeeper", "finance", "finance", "bookkeeping, reconciliations, and financial recordkeeping"),
    _role("Payroll Specialist", "finance", "finance", "payroll processing, compliance, and employee records"),
    _role("Revenue Accountant", "finance", "finance", "revenue accounting, recognition, and financial reporting"),
    _role("Financial Analyst", "finance", "finance", "financial analysis, forecasting, and management reporting"),
    _role("FP&A Analyst", "finance", "finance", "financial planning, forecasting, and performance analysis", aliases=("Financial Planning and Analysis Analyst",)),
    _role("Billing Specialist", "finance", "finance", "billing operations, invoicing, and account reconciliation"),
    _role("Collections Specialist", "finance", "finance", "customer collections, receivables follow-up, and cash recovery"),
    _role("Tax Accountant", "finance", "finance", "tax accounting, compliance, and financial reporting"),

    # Marketing & Growth (Graphic Designer is canonical here, per Brett)
    _role("Content Marketing Specialist", "marketing", "marketing", "content strategy, editorial production, and demand generation"),
    _role("Copywriter", "marketing", "marketing", "conversion copy, campaign messaging, and brand voice"),
    _role("Digital Marketing Specialist", "marketing", "marketing", "digital campaigns, channel execution, and performance analysis"),
    _role("Email Marketing Specialist", "marketing", "marketing", "email campaigns, lifecycle journeys, and audience segmentation"),
    _role("Graphic Designer", "marketing", "marketing", "brand design, campaign creative, and digital asset production"),
    _role("Growth Marketing Specialist", "marketing", "marketing", "growth experiments, acquisition, and funnel optimization"),
    _role("Performance Marketing Manager", "marketing", "marketing", "paid acquisition, campaign optimization, and creative testing"),
    _role("Performance Marketing Specialist", "marketing", "marketing", "paid acquisition, campaign optimization, and performance analysis"),
    _role("Paid Media Specialist", "marketing", "marketing", "paid media, audience targeting, and campaign optimization"),
    _role("PPC Specialist", "marketing", "marketing", "paid search, keyword strategy, and campaign optimization", aliases=("Pay Per Click Specialist",)),
    _role("SEO Specialist", "marketing", "marketing", "search optimization, content performance, and organic growth"),
    _role("Social Media Manager", "marketing", "marketing", "social strategy, content publishing, and audience engagement"),
    _role("Marketing Coordinator", "marketing", "marketing", "campaign coordination, marketing operations, and content execution"),
    _role("Lifecycle Marketing Specialist", "marketing", "marketing", "lifecycle journeys, customer segmentation, and retention marketing"),
    _role("CRM Marketing Specialist", "marketing", "marketing", "CRM campaigns, lifecycle automation, and audience segmentation"),
    _role("Brand Manager", "marketing", "marketing", "brand strategy, campaign execution, and cross-channel consistency"),
    _role("Marketing Analyst", "marketing", "marketing", "marketing analytics, attribution, and performance reporting"),
    _role("Marketing Automation Specialist", "marketing", "marketing", "marketing automation, lifecycle workflows, and CRM operations"),
    _role("Product Marketing Specialist", "marketing", "marketing", "product positioning, go-to-market execution, and sales enablement"),

    # Operations & Administration
    _role("Business Operations Specialist", "operations", "operations", "business operations, process improvement, and cross-functional execution"),
    _role("Executive Assistant", "operations", "operations", "executive support, calendar management, and operational coordination"),
    _role("Administrative Assistant", "operations", "operations", "administrative support, scheduling, and document coordination"),
    _role("Operations Analyst", "operations", "operations", "operations analysis, process improvement, and performance reporting"),
    _role("Virtual Assistant", "operations", "operations", "remote administrative support, scheduling, and workflow coordination"),
    _role("Project Coordinator", "operations", "operations", "project coordination, stakeholder follow-up, and delivery tracking"),
    _role("Data Entry Specialist", "operations", "operations", "data entry, record accuracy, and administrative processing"),
    _role("Customer Operations Specialist", "operations", "operations", "customer operations, process coordination, and service delivery"),

    # People & HR
    _role("Benefits Administrator", "people_hr", "people_hr", "benefits administration, employee support, and HR compliance"),
    _role("Compensation Analyst", "people_hr", "people_hr", "compensation analysis, benchmarking, and people reporting"),
    _role("HR Administrator", "people_hr", "people_hr", "HR administration, employee records, and people operations"),
    _role("HR Analyst", "people_hr", "people_hr", "people analytics, HR reporting, and workforce insights"),
    _role("HR Generalist", "people_hr", "people_hr", "employee support, HR operations, and policy administration"),
    _role("HR Operations Specialist", "people_hr", "people_hr", "HR operations, employee lifecycle workflows, and compliance"),
    _role("Recruiter", "people_hr", "people_hr", "candidate sourcing, recruiting operations, and hiring coordination"),
    _role("Technical Recruiter", "people_hr", "people_hr", "technical recruiting, candidate sourcing, and hiring coordination"),
    _role("Talent Acquisition Specialist", "people_hr", "people_hr", "talent acquisition, candidate sourcing, and hiring operations"),
    _role("Recruiting Coordinator", "people_hr", "people_hr", "interview coordination, candidate experience, and recruiting operations"),
    _role("People Operations Specialist", "people_hr", "people_hr", "people operations, employee lifecycle support, and HR systems"),
    _role("Learning & Development Specialist", "people_hr", "people_hr", "learning programs, employee development, and training operations", aliases=("Learning and Development Specialist",)),

    # Sales & Revenue Operations
    _role("Account Executive", "gtm_revenue", "gtm_revenue", "pipeline development, consultative selling, and revenue growth"),
    _role("Account Manager", "gtm_revenue", "gtm_revenue", "account management, renewals, and customer expansion"),
    _role("Business Development Representative", "gtm_revenue", "gtm_revenue", "prospecting, qualification, and pipeline generation", aliases=("BDR",)),
    _role("Sales Development Representative", "gtm_revenue", "gtm_revenue", "prospecting, qualification, and pipeline generation", aliases=("SDR",)),
    _role("Inside Sales Representative", "gtm_revenue", "gtm_revenue", "inside sales, prospect qualification, and revenue generation"),
    _role("Lead Generation Specialist", "gtm_revenue", "gtm_revenue", "lead generation, prospect research, and outbound execution"),
    _role("Deal Desk Analyst", "gtm_revenue", "gtm_revenue", "deal operations, pricing support, and sales process governance"),
    _role("CRM Administrator", "gtm_revenue", "gtm_revenue", "CRM administration, data quality, and revenue workflows"),
    _role("Sales Operations Analyst", "gtm_revenue", "gtm_revenue", "sales operations, pipeline reporting, and process optimization"),
    _role("Revenue Operations Analyst", "gtm_revenue", "gtm_revenue", "revenue operations, funnel analysis, and GTM systems"),
    _role("Sales Enablement Specialist", "gtm_revenue", "gtm_revenue", "sales enablement, content management, and rep productivity"),
    _role("Partnerships Manager", "gtm_revenue", "partnerships", "strategic partnerships, partner development, and channel growth"),

    # AI Talent
    _role(
        "AI Automation Engineer",
        "engineering",
        "engineering",
        "AI automation, agent workflows, and systems integrations",
        negative_patterns=(
            r"\b(?:industrial|manufacturing) automation\b",
            r"\b(?:plc|scada|controls engineer|instrumentation)\b",
        ),
    ),
    _role("AI Engineer", "engineering", "engineering", "AI systems, LLM integrations, and production automation"),
    _role("GTM Engineer", "gtm_revenue", "gtm_revenue", "GTM systems, workflow automation, and revenue operations"),
    _role("AI Content Specialist", "marketing", "marketing", "AI-assisted content production, editorial workflows, and content optimization"),
    _role("Prompt Engineer", "engineering", "engineering", "prompt systems, LLM evaluation, and AI workflow optimization"),
    _role("AI Operations Specialist", "operations", "operations", "AI operations, workflow implementation, and process optimization"),
    _role("Machine Learning Engineer", "engineering", "engineering", "machine learning systems, model deployment, and production reliability", aliases=("ML Engineer",)),
    _role("AI Data Annotator", "engineering", "data", "AI data annotation, quality review, and training-data operations"),
    _role("Data Labeling Specialist", "engineering", "data", "data labeling, quality review, and training-data operations", aliases=("Data Labelling Specialist",)),
    _role("Automation Specialist", "engineering", "engineering", "workflow automation, systems integrations, and process optimization"),
    _role("Chatbot Specialist", "engineering", "engineering", "chatbot workflows, conversational design, and AI integrations"),
    _role("Conversational AI Specialist", "engineering", "engineering", "conversational AI, chatbot workflows, and customer automation"),

    # Design & Creative. Graphic Designer intentionally lives only in Marketing.
    _role("UX/UI Designer", "product", "product", "user experience, interface design, and product usability", aliases=("UI/UX Designer", "UX Designer", "UI Designer")),
    _role("Motion Designer", "marketing", "marketing", "motion design, campaign creative, and visual storytelling"),
    _role("Web Designer", "marketing", "marketing", "web design, landing pages, and conversion-focused user experience"),
    _role("Video Editor", "marketing", "marketing", "video editing, post-production, and social content"),
    _role("Video Producer", "marketing", "marketing", "video production, creative development, and content delivery"),
    _role("Content Writer", "marketing", "marketing", "content writing, editorial production, and brand messaging"),
    _role("Podcast Editor", "marketing", "marketing", "podcast editing, audio post-production, and episode delivery"),
    _role("Podcast Producer", "marketing", "marketing", "podcast production, guest coordination, and episode delivery"),

    # Product
    _role("Product Analyst", "product", "product", "product analytics, user insights, and roadmap decision support"),
    _role("Product Designer", "product", "product", "product design, user experience, and interface systems"),
    _role("Technical Writer", "product", "product", "technical documentation, product education, and knowledge management"),
    _role("Product Support Specialist", "product", "product", "product support, technical troubleshooting, and customer education"),

    # E-commerce
    _role("E-commerce Manager", "ecommerce", "ecommerce", "e-commerce operations, channel growth, and conversion optimization", aliases=("Ecommerce Manager",)),
    _role("Amazon Marketplace Specialist", "ecommerce", "ecommerce", "Amazon marketplace operations, listings, and channel growth"),
    _role("Shopify Developer", "ecommerce", "ecommerce", "Shopify development, storefront integrations, and site performance"),
    _role("Shopify Specialist", "ecommerce", "ecommerce", "Shopify operations, storefront optimization, and merchandising"),
    _role("Catalog Specialist", "ecommerce", "ecommerce", "product catalog management, data accuracy, and merchandising", negative_patterns=(r"\blibrary\b",)),
    _role("Listings Specialist", "ecommerce", "ecommerce", "product listings, marketplace optimization, and catalog accuracy"),
]


ROLE_DEFINITIONS: Dict[str, RoleDefinition] = {
    definition.canonical_title: definition for definition in _ROLE_DEFINITIONS
}

if len(ROLE_DEFINITIONS) != len(_ROLE_DEFINITIONS):
    raise RuntimeError("Duplicate canonical role titles in role catalog")


def _normalize_title(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


_ROLE_LOOKUP: Dict[str, str] = {}
for _definition in _ROLE_DEFINITIONS:
    for _term in _definition.match_terms:
        _key = _normalize_title(_term)
        existing = _ROLE_LOOKUP.get(_key)
        if existing and existing != _definition.canonical_title:
            raise RuntimeError(
                f"Role alias {_term!r} maps to both {existing!r} and "
                f"{_definition.canonical_title!r}"
            )
        _ROLE_LOOKUP[_key] = _definition.canonical_title


DEFAULT_SEARCH_ROLES: Tuple[str, ...] = tuple(ROLE_DEFINITIONS)


def canonical_role_for_search(search_title: str) -> str:
    """Resolve a configured search title/alias to a catalog canonical title.

    Unknown values remain usable for backward-compatible custom ROLES_JSON
    experiments; relevance will route them to human review instead of crashing.
    """
    return _ROLE_LOOKUP.get(_normalize_title(search_title), search_title.strip())


def get_role_definition(role_title: str) -> Optional[RoleDefinition]:
    canonical = canonical_role_for_search(role_title)
    return ROLE_DEFINITIONS.get(canonical)


def get_function_bucket(role_title: str, default: str = "gtm_revenue") -> str:
    definition = get_role_definition(role_title)
    return definition.function_bucket if definition else default


def get_hiring_manager_bucket(role_title: str, default: str = "gtm_revenue") -> str:
    definition = get_role_definition(role_title)
    return definition.hiring_manager_bucket if definition else default


def get_fallback_focus(role_title: str) -> str:
    definition = get_role_definition(role_title)
    return definition.fallback_focus if definition else ""


def role_specificity(role_title: str) -> int:
    """Length-based tie breaker so Tax Accountant beats Accountant, etc."""
    definition = get_role_definition(role_title)
    terms = definition.match_terms if definition else (role_title,)
    return max((len(_normalize_title(term).split()) * 100 + len(_normalize_title(term)) for term in terms), default=0)
