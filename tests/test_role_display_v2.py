from __future__ import annotations

import copy
import unittest

from role_display_resolver import RESOLVER_VERSION, resolve_role_display


class RoleDisplayV2RepresentativeTests(unittest.TestCase):
    def test_audited_context_and_noise_categories(self):
        cases = (
            # Product/domain specialization.
            ("Software Engineer – Conversational AI", "Software Engineer", "Software Engineer"),
            ("Account Executive – Electronic Security Systems", "Account Executive", "Account Executive"),
            ("Account Executive — AI Readiness", "Account Executive", "Account Executive"),
            # Company, team, and business-unit context.
            ("Marketing Coordinator, Disney Theatrical Group", "Marketing Coordinator", "Marketing Coordinator"),
            ("People Development Team - TEMP Recruiter", "Recruiter", "Recruiter"),
            # Location and work arrangement.
            ("Enterprise Account Executive - North America", "Account Executive", "Enterprise Account Executive"),
            ("Staff Accountant- REMOTE", "Staff Accountant", "Staff Accountant"),
            (
                "Technical Account Executive AI-Driven Energy Management (Remote from U.S.)",
                "Account Executive",
                "Technical Account Executive",
            ),
            # Language requirement and territory.
            (
                "Bilingual Junior Account Executive - Phoenix, AZ (Southwest)",
                "Account Executive",
                "Junior Account Executive",
            ),
            # Role Focus context.
            ("HR Generalist - Learning and Development", "HR Generalist", "HR Generalist"),
            # Promotional and benefits copy.
            ("Account Executive - Now Hiring", "Account Executive", "Account Executive"),
            ("Senior Condominium Community Manager | Flexible PTO", "Community Manager", "Senior Community Manager"),
            # ATS/source and requisition text.
            (
                "15016 - Business Development Representative - North Florida",
                "Business Development Representative",
                "Business Development Representative",
            ),
            ("Graphic Designer Job at Bedgear in Farmingdale", "Graphic Designer", "Graphic Designer"),
            # Salary and posting metadata.
            (
                "Remote Sr Product Designer 130 160k FinTech 38",
                "Product Designer",
                "Senior Product Designer",
            ),
            # Redundant posting words and multiple-opening promotional copy.
            (
                "Founding & Lead Recruiter Roles | AI Startups | $110K–$210K+",
                "Recruiter",
                "Lead Recruiter",
            ),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({
                    "job_title": raw,
                    "_matched_role": matched,
                    "role_focus": "Structured context retained here",
                })
                self.assertEqual(result.name, expected)
                self.assertFalse(result.hold)
                self.assertIn(result.confidence, {"high", "medium"})

    def test_serialized_duplicates_and_conventional_abbreviations(self):
        cases = (
            ('["Video Producer","Video Producer"]', "Video Producer", "Video Producer"),
            ("Jr Accountant", "Accountant", "Junior Accountant"),
            ("Sr. Marketing Manager", "Marketing Manager", "Senior Marketing Manager"),
            ("Billing Specialist ll", "Billing Specialist", "Billing Specialist II"),
            ("SALES DEVELOPMENT REPRESENTATIVE", "Sales Development Representative", "Sales Development Representative"),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, expected)
                self.assertFalse(result.hold)

    def test_conventional_modifiers_are_preserved(self):
        cases = (
            ("Enterprise Account Executive", "Account Executive"),
            ("Mid-Market Sales Development Representative", "Sales Development Representative"),
            ("Technical Account Manager", "Account Manager"),
            ("Senior Software Engineer", "Software Engineer"),
            ("Staff Accountant", "Accountant"),
            ("Founding Account Executive", "Account Executive"),
            ("Lead Recruiter", "Recruiter"),
            ("Head of Customer Success", "Customer Success"),
            ("Principal Technical Account Manager", "Account Manager"),
            ("Strategic Enterprise Account Manager", "Account Manager"),
            ("Commercial Account Executive", "Account Executive"),
            ("Key Account Manager", "Account Manager"),
            ("National Account Manager", "Account Manager"),
            ("Regional Account Executive", "Account Executive"),
            ("Channel Account Manager", "Account Manager"),
            ("Inside Sales Account Manager", "Account Manager"),
            ("Senior Technical Recruiter", "Recruiter"),
            ("Cost Accountant", "Accountant"),
            ("Full Charge Bookkeeper", "Bookkeeper"),
        )
        for raw, matched in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, raw)
                self.assertFalse(result.hold)
                self.assertEqual(result.confidence, "high")

    def test_market_modifier_can_be_promoted_from_posting_context(self):
        cases = (
            ("Account Executive - SMB Markets", "Account Executive", "SMB Account Executive"),
            ("Entry-Level Inside Sales Representative", "Inside Sales Representative", "Junior Inside Sales Representative"),
            ("Account Executive, Mid-Market (Logistics SaaS)", "Account Executive", "Mid-Market Account Executive"),
            (
                "Sales Development Representative (AI-Native, Mid-Market)",
                "Sales Development Representative",
                "Mid-Market Sales Development Representative",
            ),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, expected)
                self.assertFalse(result.hold)

    def test_role_specific_conventional_variants_are_preserved(self):
        cases = (
            ("Corporate Property Accountant", "Accountant", "Property Accountant"),
            ("Accounts Payable Accountant", "Accountant", "Accounts Payable Accountant"),
            ("Senior Software Engineer, Backend", "Software Engineer", "Senior Backend Software Engineer"),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, expected)
                self.assertFalse(result.hold)


class RoleDisplayV2SafetyTests(unittest.TestCase):
    def test_competing_role_heads_are_ambiguous_and_held(self):
        cases = (
            ("Payroll Administrator/General Ledger Accountant", "Accountant"),
            ("Sales Account Manager/ Broker", "Account Manager"),
            ("Customer Care Associate (Business Development Representative)", "Business Development Representative"),
            ("Revenue Operations Manager / HubSpot CRM Administrator", "CRM Administrator"),
        )
        for raw, matched in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, raw)
                self.assertTrue(result.ambiguous)
                self.assertTrue(result.hold)
                self.assertEqual(result.confidence, "low")
                self.assertEqual(result.evidence["status"], "AMBIGUOUS")

    def test_related_operations_classifier_keeps_clean_actual_title(self):
        result = resolve_role_display({
            "job_title": "CRM Manager (Remote)",
            "_matched_role": "Automation Specialist",
        })
        self.assertEqual(result.name, "CRM Manager")
        self.assertFalse(result.hold)
        self.assertEqual(result.confidence, "medium")

    def test_missing_matched_role_uses_natural_title_conservatively(self):
        result = resolve_role_display({
            "job_title": "AI Implementation Specialist (Contech) - Remote",
            "role_focus": "Construction technology implementation",
        })
        self.assertEqual(result.name, "AI Implementation Specialist")
        self.assertEqual(result.confidence, "medium")
        self.assertFalse(result.hold)
        self.assertEqual(result.evidence["anchor_method"], "missing_matched_role")

    def test_missing_matched_role_recognizes_audited_conventional_heads(self):
        cases = (
            ("Senior Underwriting Counsel", {}, "Senior Underwriting Counsel"),
            ("Senior People Partner (HRBP) - India", {"job_location": "India"}, "Senior People Partner"),
            ("LATAM Medical Lead", {}, "Medical Lead"),
            ("Senior Associate Software Engineer", {}, "Senior Associate Software Engineer"),
            ("Partner Relationship Manager - Strategic Technology Partnerships", {}, "Partner Relationship Manager"),
            ("Associate Observability Architect | EST | Remote", {}, "Associate Observability Architect"),
        )
        for raw, extra, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, **extra})
                self.assertEqual(result.name, expected)
                self.assertEqual(result.confidence, "medium")
                self.assertFalse(result.hold)

    def test_missing_anchor_fallback_keeps_role_modifiers_and_drops_only_safe_context(self):
        cases = (
            ("Account Executive (Enterprise) - West Region", "Enterprise Account Executive"),
            ("Sr. Developer (Front End)", "Senior Frontend Developer"),
            ("Senior People Partner (HRBP) - India", "Senior People Partner"),
            ("Operations Specialist II, Aerospace & Defense (US)", "Operations Specialist II"),
            ("Partner Relationship Manager - Strategic Technology Partnerships", "Partner Relationship Manager"),
            ("Data Center Hardware Technician - Lockport, NY (on-site)", "Data Center Hardware Technician"),
            ("Remote Sales Representative Acute Care & Monitoring Denmark & Norway", "Sales Representative"),
            ("Part Time Remote Licensed Talk Therapist - Fee For Service", "Licensed Talk Therapist"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw})
                self.assertEqual(result.name, expected)
                self.assertEqual(result.confidence, "medium")
                self.assertFalse(result.hold)

    def test_missing_anchor_does_not_blindly_split_a_generic_role(self):
        result = resolve_role_display({"job_title": "Manager, Research Ethics and Compliance"})
        self.assertEqual(result.name, "Manager, Research Ethics and Compliance")
        self.assertFalse(result.hold)

    def test_related_broader_classifier_keeps_the_actual_clean_title(self):
        cases = (
            ("Paid Media Manager", "Performance Marketing Manager", "Paid Media Manager"),
            ("Marketing Operations Manager", "Marketing Automation Specialist", "Marketing Operations Manager"),
            ("Product Support Rep", "Customer Support", "Product Support Representative"),
            ("Search Engine Optimization (SEO) Analyst", "SEO Specialist", "Search Engine Optimization Analyst"),
            ("Sales Enablement Lead", "Business Development Representative", "Sales Enablement Lead"),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, expected)
                self.assertEqual(result.confidence, "medium")
                self.assertFalse(result.hold)

    def test_related_classifier_drops_delimited_role_focus_context(self):
        cases = (
            ("Remote B2B Marketing Manager - SaaS Growth & Strategy", "Performance Marketing Manager", "B2B Marketing Manager"),
            ("Performance Marketing Specialist — Paid Social & Optimization", "Performance Marketing Manager", "Performance Marketing Specialist"),
        )
        for raw, matched, expected in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertEqual(result.name, expected)
                self.assertEqual(result.confidence, "medium")
                self.assertFalse(result.hold)

    def test_conventional_client_success_alias_corroborates_customer_success_anchor(self):
        result = resolve_role_display({
            "job_title": "Client Success Manager Submetering SaaS Growth (Hybrid)",
            "_matched_role": "Customer Success Manager",
        })
        self.assertEqual(result.name, "Customer Success Manager")
        self.assertEqual(result.confidence, "high")
        self.assertFalse(result.hold)

    def test_cross_function_disagreement_or_compound_heads_still_hold(self):
        cases = (
            ("Digital Marketing Manager", "Customer Support"),
            ("Copywriter II - REMOTE", "Automation Specialist"),
            ("Remote Digital Campaign Manager Global Paid Media Lead", "Performance Marketing Manager"),
        )
        for raw, matched in cases:
            with self.subTest(raw=raw):
                result = resolve_role_display({"job_title": raw, "_matched_role": matched})
                self.assertTrue(result.hold)
                self.assertEqual(result.evidence["status"], "AMBIGUOUS")

    def test_missing_matched_role_with_competing_heads_is_held(self):
        result = resolve_role_display({"job_title": "Office Manager / HR Associate"})
        self.assertTrue(result.hold)
        self.assertEqual(result.evidence["status"], "AMBIGUOUS")

    def test_missing_role_focus_does_not_change_a_corroborated_decision(self):
        without_focus = resolve_role_display({
            "job_title": "Account Executive — AI Readiness",
            "_matched_role": "Account Executive",
        })
        with_focus = resolve_role_display({
            "job_title": "Account Executive — AI Readiness",
            "_matched_role": "Account Executive",
            "role_focus": "AI readiness",
        })
        self.assertEqual(without_focus.name, "Account Executive")
        self.assertEqual(without_focus.name, with_focus.name)
        self.assertFalse(without_focus.hold)

    def test_broader_matched_role_keeps_natural_single_title(self):
        result = resolve_role_display({
            "job_title": "Technical Support Specialist [Sat - Wed]",
            "_matched_role": "Customer Support",
        })
        self.assertEqual(result.name, "Technical Support Specialist")
        self.assertEqual(result.confidence, "medium")
        self.assertFalse(result.hold)

    def test_input_and_canonical_fields_are_never_mutated(self):
        job = {
            "job_title": "Account Executive — AI Readiness",
            "canonical_job_title": "Account Executive — AI Readiness",
            "_matched_role": "Account Executive",
            "_role_bucket": "gtm_revenue",
            "role_focus": "AI readiness",
        }
        before = copy.deepcopy(job)
        result = resolve_role_display(job)
        self.assertEqual(result.resolver_version, RESOLVER_VERSION)
        self.assertEqual(result.name, "Account Executive")
        self.assertEqual(job, before)


if __name__ == "__main__":
    unittest.main()
