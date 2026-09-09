"""SYNTHETIC stratified corpus: nine campaigns × positives and hard negatives.

Labels were written by hand as part of this branch; this corpus gates regressions of
the deterministic layer and drives the demo. It is NOT the independent acceptance
corpus required by ACCEPTANCE.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Example:
    key: str
    title: str
    description: str
    expected_function: Optional[str]   # None = must not be compatible with any function
    expected_excluded: bool = False
    note: str = ""


CORPUS: List[Example] = [
    # --- customer_success ---------------------------------------------------
    Example("cs1", "Customer Success Manager",
            "You will own customer onboarding for new accounts and drive product adoption across your book of business. "
            "Run quarterly business reviews with executive sponsors, track health scores, and lead renewals and expansion "
            "conversations. Act as the trusted advisor and voice of the customer with product. Full-time, remote within the US.",
            "customer_success"),
    Example("cs2", "Team Member",
            "In this role you will lead customer onboarding and implementation for new customers, coach them toward product "
            "adoption, and manage a portfolio of accounts through renewals. You'll prepare quarterly business reviews and "
            "monitor health scores to reduce churn. This is a full-time role and remote within the United States.",
            "customer_success", note="generic title, same work"),
    # --- customer_support ---------------------------------------------------
    Example("sup1", "Customer Support Specialist",
            "Respond to customer inquiries via email, chat and phone, resolve support tickets in Zendesk within SLA, "
            "troubleshoot product issues, escalate complex tickets to engineering and maintain the knowledge base. "
            "Full-time, remote (US).", "customer_support"),
    Example("sup2", "Specialist",
            "Work the ticket queue in Intercom, respond to user questions across chat and email, troubleshoot technical "
            "issues, keep first response time within SLA and write help center articles. Full-time position, US remote.",
            "customer_support"),
    # --- engineering --------------------------------------------------------
    Example("eng1", "Software Engineer",
            "Design, build and maintain APIs and backend services in Python and TypeScript. Own CI/CD pipelines with "
            "Docker and Kubernetes on AWS, write unit and integration tests, participate in code reviews and improve "
            "system design for scalability. Full-time, remote in the US.", "engineering"),
    Example("eng2", "Automation Builder",
            "You will build LLM-powered agents and RAG pipelines, integrate large language models into our product with "
            "Python, ship features behind automated tests, and automate internal workflows with scripts and APIs. "
            "Full-time, fully remote, US-based.", "engineering", note="no catalogue title"),
    # --- finance ------------------------------------------------------------
    Example("fin1", "Staff Accountant",
            "Own accounts payable and accounts receivable, prepare journal entries and reconciliations for the month-end "
            "close, maintain the general ledger in NetSuite, support financial reporting under GAAP and assist with the "
            "annual audit. Full-time, remote within the US.", "finance", note="'Staff Accountant' is an IC title, not a Staff Engineer level"),
    Example("fin2", "Accounting Associate",
            "Own accounts payable and accounts receivable, prepare journal entries and reconciliations for the month-end "
            "close, maintain the general ledger in NetSuite, support financial reporting under GAAP and assist with the "
            "annual audit. Full-time, remote within the US.", "finance"),
    Example("fin3", "Operations Generalist",
            "Handle bookkeeping in QuickBooks, process invoicing and collections, run weekly reconciliations, help with "
            "budgeting and forecasting, and prepare monthly financial statements for leadership. Full-time, US remote.",
            "finance", note="ops-sounding title, finance work"),
    # --- people_hr ----------------------------------------------------------
    Example("hr1", "People Operations Coordinator",
            "Run employee onboarding and offboarding, administer benefits enrollment in Rippling, keep the HRIS accurate, "
            "support full-cycle recruiting and candidate experience, and ensure compliance with labor laws including I-9 and "
            "FMLA. Full-time, remote in the United States.", "people_hr"),
    Example("hr2", "Coordinator",
            "You'll own the employee lifecycle from onboarding through offboarding, coordinate open enrollment and benefits "
            "administration, source candidates and manage the candidate pipeline, and keep HR policies and the employee "
            "handbook current. Full-time, US remote.", "people_hr"),
    # --- marketing ----------------------------------------------------------
    Example("mkt1", "Growth Marketer",
            "Run paid media across Google Ads and Meta ads, build lifecycle marketing and nurture campaigns in HubSpot, "
            "own the content calendar and SEO, manage social media channels and report on campaign performance and "
            "attribution. Full-time, remote (US).", "marketing"),
    Example("mkt2", "Creative Producer",
            "Produce brand and creative assets in Figma and Adobe, maintain brand guidelines, edit short video, support "
            "email marketing and social media content, and A/B test landing pages with the growth team. Full-time role, "
            "remote within the US.", "marketing"),
    # --- operations ---------------------------------------------------------
    Example("ops1", "Business Operations Associate",
            "Document and improve internal processes, write SOPs and process documentation, manage vendor relationships "
            "and procurement, coordinate cross-functional projects and timelines in Asana, and track operational metrics "
            "and OKRs. Full-time, US remote.", "operations"),
    Example("ops2", "Executive Assistant",
            "Provide executive support to the COO: calendar management, scheduling meetings and travel, expense reports, "
            "and coordinating cross-functional projects. Improve day-to-day operations and document standard operating "
            "procedures. Full-time, remote within the US.", "operations"),
    # --- product ------------------------------------------------------------
    Example("prod1", "Product Manager",
            "Own the product roadmap and write product requirements and PRDs, run user research and user interviews, "
            "prioritize the backlog with engineering and design, define product metrics in Amplitude and lead sprint "
            "planning. Full-time, remote in the US.", "product"),
    Example("prod2", "Product Designer",
            "Create wireframes, prototypes and high-fidelity mockups in Figma, maintain our design system, run usability "
            "testing and user research, and partner with engineering and product to ship features. Full-time, US remote.",
            "product"),
    # --- gtm_revenue --------------------------------------------------------
    Example("gtm1", "GTM Engineer",
            "Own Salesforce administration and CRM automation, build lead routing and lead scoring with round-robin "
            "assignment, run lead enrichment in Clay and Apollo.io, maintain outbound sequences in Outreach.io and build "
            "pipeline reporting and forecasting dashboards. Full-time, remote (US).", "gtm_revenue"),
    Example("gtm2", "Revenue Operations Analyst",
            "Support revenue operations: HubSpot workflows and CRM hygiene, territory and lead routing rules, sales "
            "forecasting and pipeline analytics, quota and commission plan administration. Full-time, US remote.",
            "gtm_revenue"),
    # --- ecommerce ----------------------------------------------------------
    Example("ecom1", "Ecommerce Specialist",
            "Manage our Shopify online store: product listings and catalog, merchandising, promotions and discount codes, "
            "Amazon Seller Central marketplace listings, and conversion rate optimization of the checkout flow. "
            "Full-time, remote within the US.", "ecommerce"),
    Example("ecom2", "Digital Merchandiser",
            "Own product detail pages and site merchandising on BigCommerce, sync inventory levels, manage marketplace "
            "listings on Walmart Marketplace and Amazon FBA, and improve site search and navigation for our DTC brand. "
            "Full-time, US remote.", "ecommerce"),
    # --- hard negatives -----------------------------------------------------
    Example("neg1", "Product Manager",
            "Operate a forklift and pallet jack in the warehouse, must work in the warehouse daily, lift up to 50 lbs, "
            "and manage product listings for inventory counts. Full-time.", None, expected_excluded=True,
            note="attractive title, physical work"),
    Example("neg2", "Director of Finance",
            "Lead a team of accountants, own month-end close, financial reporting and FP&A, manage 4 direct reports and "
            "present to the board. Full-time, remote in the US.", None, expected_excluded=True, note="leadership"),
    Example("neg3", "Customer Success Manager (Contract)",
            "This is a 6-month contract role. Own customer onboarding, product adoption and renewals for a portfolio of "
            "accounts, run quarterly business reviews. Remote within the US.", None, expected_excluded=True, note="contract"),
    Example("neg4", "Marketing Manager",
            "Run paid media and lifecycle marketing for our EMEA region. Candidates must be based in Europe; EMEA only. "
            "Full-time.", None, expected_excluded=True, note="foreign only"),
    Example("neg5", "Recruiter",
            "We are a staffing agency that connects talent with employers. Our client is seeking a full-cycle recruiter to "
            "source candidates for multiple client accounts. Full-time, US remote.", None, expected_excluded=True, note="agency"),
    Example("neg6", "Software Engineer",
            "Active Top Secret security clearance is required. Build backend services in Java for a federal agency program. "
            "Full-time, on-site.", None, expected_excluded=True, note="clearance"),
    Example("neg7", "Operations Associate",
            "Great opportunity. Apply now.", None, expected_excluded=False, note="no evidence -> insufficient, never approved"),
    Example("neg8", "Head of Customer Support",
            "Manage and lead a team of 12 support specialists, conduct performance reviews, own hiring and coaching, "
            "and set the support strategy. Full-time, US remote.", None, expected_excluded=True, note="people management"),
]
