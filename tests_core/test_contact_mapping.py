"""tgtc-contact/1: every case is a calibration mapping (cal#N, contact_pilot_20260919/qa_sheet.txt) or an
explicit rule from the 2026-09-19 instruction. No holdout mapping is used here."""
import pytest

from tgtc_core.domain import contact_mapping as cm

ENG, GTM, MKT, CS = cm.GROUPS


def decide(title, group, job, li=None, apollo=None):
    return cm.map_contact(contact_title=title, offer_group=group, job_title=job,
                          linkedin_headcount=li, apollo_employees=apollo)


@pytest.mark.parametrize("title,level", [
    ("VP Engineering", "vp"), ("Vice President, Engineering", "vp"), ("Vice-President of Sales", "vp"),
    ("SVP Marketing", "svp"), ("Senior Vice President of Data", "svp"), ("EVP, Technology", "evp"),
    ("Executive Vice President, Sales", "evp"), ("Head of Customer Success", "head"), ("Engineering Head", "head"),
    ("Director of Growth Marketing", "director"), ("AVP, Marketing", "director"), ("CTO", "c_level"),
    ("Chief Customer & Growth Officer", "c_level"), ("Chief of Staff", None), ("Senior Manager", None),
])
def test_equivalent_seniority_expressions(title, level):
    assert cm.seniority(title) == level


@pytest.mark.parametrize("title", ["Vice President of Sales", "Senior Vice President, Marketing",
                                   "Executive Vice President", "Assistant Vice President, IT"])
def test_vice_president_is_never_founder_tier(title):
    assert not cm.is_small_company_executive(title)


@pytest.mark.parametrize("title", ["Founder", "Co-Founder & CEO", "Owner/CEO", "President", "COO",
                                   "Chief Operating Officer", "Co-Ceo / Head of Creative"])
def test_small_company_executives(title):
    assert cm.is_small_company_executive(title)


def test_vice_president_of_sales_maps_as_sales_executive_above_99_employees():
    d = decide("Vice President of Sales", GTM, "Sales Operations Coordinator", li=300, apollo=320)
    assert d.accepted and d.tier == "function_executive"


def test_whole_phrases_not_substrings():
    assert not cm._any(("it",), "digital marketing")
    assert not cm._any(("data",), "database administrator")
    assert cm._any(("it",), "director of it")


@pytest.mark.parametrize("title,job,group,reason", [
    ("Vice President, Brand & Commercial Finance", "Social Media Manager", MKT, "role:other_business_function"),   # cal#11
    ("VP, Energy Infrastructure and Grid Development", "Senior HPC Engineer - Fleet Engineering", ENG, "role:senior_other_function"),  # cal#16
    ("Head of Market Data Services", "Senior QA Engineer, Platform", ENG, "role:senior_other_function"),           # cal#17
    ("Head of Go-to-market Engineering", "Senior QA Engineer, Platform", ENG, "role:senior_other_function"),       # cal#18
    ("Vice President, Sales Engineering", "AI Operations Analyst", ENG, "role:senior_other_function"),             # cal#26
    ("Regional Sales Director", "Commercial Strategy & Operations Lead", GTM, "role:regional_director_not_functional_owner"),  # cal#38
    ("Director of Purchasing Support Operations", "Applications Support Analyst", CS, "role:other_business_function"),  # cal#47
])
def test_calibration_keyword_leaks_are_rejected(title, job, group, reason):
    d = decide(title, group, job, li=300, apollo=300)
    assert not d.accepted and d.reason == reason


@pytest.mark.parametrize("title,job,group,li,apollo", [
    ("Senior Vice President of Data", "Sr. AI Engineer, CX", ENG, 307, 307),                  # cal#5
    ("Chief Technology Officer", "Staff Frontend Engineer", ENG, None, 40),                  # cal#7
    ("Director of Growth Marketing (DM)", "Digital Marketing Specialist", MKT, 134, 134),     # cal#8
    ("SVP Frida Brand", "Social Media Manager", MKT, 288, 288),                              # cal#12
    ("President, Deadline & Chief Revenue Officer, Gold Derby", "PMC: Sales Operations Coordinator", GTM, 329, 329),  # cal#14
    ("Head of Customer Support", "Technical Support Engineer", CS, 166, 166),                 # cal#22
    ("Co-Ceo / Head of Creative", "Wholesale Account Manager", CS, 38, 39),                   # cal#23
    ("Chief Customer Officer & Founder", "Account Manager (Performance Marketing)", CS, 246, 246),  # cal#28
    ("President", "Customer Service Specialist", CS, 84, 84),                                 # cal#39
    ("Founder/CEO", "Senior Account Manager", CS, 31, 28),                                   # cal#44
    ("Director of Marketing", "BLAINE BROTHERS - Marketing Content Specialist", MKT, 81, 81),  # cal#49
    ("SVP, Customer Success", "Customer Success Manager", CS, 214, 214),                     # cal#55
])
def test_calibration_correct_mappings_are_kept(title, job, group, li, apollo):
    assert decide(title, group, job, li=li, apollo=apollo).accepted


def test_division_page_is_not_a_small_company():                                             # cal#33
    d = decide("Chief Executive Officer", CS, "National Account Manager, eCommerce", li=28, apollo=850)
    assert not d.accepted and d.reason == "role:executive_not_allowed_for_size"


def test_account_management_is_not_routed_to_technical_support():                             # cal#32
    assert cm.opening_route(CS, "National Account Manager, eCommerce") == ("account_management", ("cs_leadership", "sales"))
    assert not decide("Director, Building Envelope Technical Support", CS, "National Account Manager, eCommerce",
                      li=28, apollo=850).accepted


@pytest.mark.parametrize("job,route", [
    ("Applications Support Analyst", "it_helpdesk"), ("IT Helpdesk Coordinator", "it_helpdesk"),
    ("Service Desk Analyst", "it_helpdesk"), ("Strategic Account Manager", "account_management"),
    ("Inside Sales Representative", "sales"), ("Customer Success Manager", "customer_success"),
    ("Technical Support Engineer", "customer_success"), ("Customer Service Representative", "customer_success"),
])
def test_cs_openings_are_routed_by_function(job, route):
    assert cm.opening_route(CS, job)[0] == route


def test_it_helpdesk_maps_to_it_leadership_not_cs():
    assert decide("Director of Information Technology", CS, "IT Support Lead", li=150, apollo=150).accepted
    assert not decide("VP Customer Success", CS, "IT Support Lead", li=150, apollo=150).accepted


def test_product_design_opening_has_no_owner_in_the_marketing_mapping():                       # cal#1
    assert not decide("VP, Sales and Marketing", MKT, "Staff Product Designer", li=718, apollo=718).accepted


def test_peer_of_a_senior_opening_is_not_its_decision_maker():                                 # cal#34, cal#9
    assert not decide("Vice President, Brand Marketing", MKT, "Vice President, Brand Marketing - Tea Tree", li=775).accepted
    assert not decide("Head of Marketing (EMEA)", MKT, "Head of Marketing, Langfuse", li=695).accepted
    assert decide("Chief Marketing Officer", MKT, "Head of Marketing, Langfuse", li=695).accepted


def test_founder_not_in_marketing_mapping():
    assert not decide("Co-Founder & CEO", MKT, "Marketing Manager", li=40, apollo=40).accepted


def test_territory_includes_asia():                                                            # cal#40, cal#9/10/21
    assert cm.territory_foreign("VP of Engineering and Project Management, Asia", "") == ["ASIA"]
    assert cm.territory_foreign("Head of Customer Success, EMEA", "")
    assert not cm.territory_foreign("VP Sales, North America", "")
    assert not cm.territory_foreign("CTO", "global engineering leader, Asia and US")


def org(slug, domain):
    return {"linkedin_url": f"https://www.linkedin.com/company/{slug}", "primary_domain": domain}


@pytest.mark.parametrize("email,job_domain,slug,apollo,evidence,expected", [
    ("x@balto.ai", "balto.ai", "baltosoftware", org("baltosoftware", "balto.ai"), [], ("exact", "employer_domain")),
    ("x@baltosoftware.com", "balto.ai", "baltosoftware", org("baltosoftware", "balto.ai"), ["balto.ai"],
     ("corroborated_alternate", "linkedin_page_spells_domain")),                               # cal#22
    ("x@coval.dev", "coval.ai", "covaldev", org("covaldev", "coval.ai"), ["coval.ai"],
     ("corroborated_alternate", "linkedin_page_spells_domain")),                               # cal#43
    ("x@jpms.com", "paulmitchell.com", "john-paul-mitchell-systems", org("john-paul-mitchell-systems", "paulmitchell.com"),
     ["paulmitchell.com"], ("quarantine", "alternate_domain_unproven")),                      # cal#34: initialism
    ("x@redventures.com", "bankrate.com", "bankrate", org("bankrate", "bankrate.com"), ["bankrate.com"],
     ("quarantine", "alternate_domain_unproven")),                                            # cal#36: parent domain
    ("x@dimensiondata.com", "datadimensions.com", "data-dimensions", org("dimension-data", "dimensiondata.com"),
     ["datadimensions.com"], ("quarantine", "apollo_org_not_proven_employer")),               # cal#25
    ("x@corp-brand.com", "brand.com", "brand", org("brand", "brand.com"), ["brand.com", "corp-brand.com"],
     ("corroborated_alternate", "recorded_company_domain")),
])
def test_alternate_domains_need_saved_same_org_evidence(email, job_domain, slug, apollo, evidence, expected):
    assert cm.email_alignment(email=email, job_domain=job_domain, job_slug=slug, apollo_org=apollo,
                              company_evidence_domains=evidence) == expected


@pytest.mark.parametrize("name,industry,description,expected", [
    ("SoGal Ventures", cm.VC_INDUSTRY, "About Lovevery. Lovevery is a fast-growing brand",
     "not_true_hiring_company:vc_attributed_portfolio_job"),
    ("Newfund", cm.VC_INDUSTRY, "Aircall is a unicorn, AI-powered customer communications platform",
     "not_true_hiring_company:vc_attributed_portfolio_job"),
    ("Solen Software Group", cm.VC_INDUSTRY, "About Solen Software Group. Solen is an evergreen holding company", None),
    ("Stealth Startup", "Software Development", "a startup", "org_unresolvable:placeholder_employer"),
    ("Confidential Company", "Manufacturing", "we build", "org_unresolvable:placeholder_employer"),
    ("Acme Robotics", "Software Development", "Acme builds robots", None),
])
def test_true_hiring_company(name, industry, description, expected):
    assert cm.hiring_company_problem(employer_name=name, employer_domain="example.com", industry=industry,
                                     recruitment_agency=False, description=description) == expected


def test_staffing_and_job_board_employers_are_not_hiring_companies():
    assert cm.hiring_company_problem(employer_name="Acme Staffing", employer_domain="acmestaffing.com",
                                     industry="Staffing and Recruiting", recruitment_agency=True,
                                     description="Acme Staffing is hiring") == "not_true_hiring_company:staffing_or_recruiting"


def test_a_person_is_bought_once_and_reused_across_openings():
    ledger = cm.EnrichmentLedger()
    assert ledger.need_purchase("p_1") is True
    assert ledger.need_purchase("p_1") is False
    assert ledger.need_purchase("p_2") is True
    assert ledger.reused == 1 and ledger.bought == {"p_1", "p_2"}
    assert cm.reuse_unit("domain:acme.com", MKT) == cm.reuse_unit("domain:acme.com", MKT)
    assert cm.reuse_unit("domain:acme.com", MKT) != cm.reuse_unit("domain:acme.com", ENG)
