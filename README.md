# TGTC AI-Powered Job Intent & Outbound Pipeline

An automated outbound pipeline that identifies companies actively hiring for roles TGTC can support, finds the appropriate hiring manager, enriches contact information, routes qualified leads through Airtable for human review, and enrolls approved leads into the correct Instantly campaign.

## Pipeline

```text
JSearch
→ Role relevance
→ Business and ICP filters
→ Company enrichment
→ Hiring manager identification
→ Apollo / Hunter contact enrichment
→ Airtable human review
→ Instantly campaign enrollment
```

## Core Capabilities

* Collects recent US job postings across the centralized Intent 2.0 role catalog
* Normalizes 100+ search titles into canonical roles, functions, and buyer hierarchies
* Matches duplicate search results to the most specific relevant role and campaign
* Excludes staffing firms, out-of-scope industries, explicit in-person/non-paying roles, non-US roles, duplicates, CRM companies, and companies outside the target employee range
* Routes technical and GTM automation roles according to job-description signals
* Separates campaign function from hiring-manager routing so Data, IT, Finance, HR, Product, E-commerce, and other roles reach the right buyer
* Identifies the most relevant hiring manager across all related openings in the same company/function
* Uses Apollo and Hunter for contact enrichment
* Generates concise, role-specific personalization from job-description signals
* Groups related openings by company and functional bucket
* Sends qualified leads to Airtable for human review
* Enrolls approved leads into the correct Instantly campaign
* Prevents duplicate Airtable records and duplicate campaign enrollment

## Workflow

### Daily Pipeline

```bash
python -u run_daily.py
```

The daily pipeline performs scraping, qualification, enrichment, and Airtable synchronization.

### Approved Lead Sync

```bash
python -u run_approved.py
```

The approval worker checks Airtable for approved leads and enrolls them into the appropriate Instantly campaign.

## Human Review

Qualified leads are organized in Airtable using four primary views:

* Ready to Approve
* Review Queue
* Enrolled
* All Leads

The reviewer validates the company, open role, role focus, hiring manager, and email before approving a lead.

## Production Deployment

The production deployment uses Railway with two scheduled services:

```text
Daily Pipeline
→ runs once per day

Approved Lead Sync
→ runs every five minutes
```

Application secrets and API credentials are configured through Railway environment variables and are not stored in the repository.

## Local Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a local `.env` using `.env.example` as the reference.

Run validation (73 tests plus catalog-wide subtests):

```bash
python -m unittest discover -s tests -v
python validate_setup.py
```

Run a controlled end-to-end test:

```bash
python -u run_full_test.py --companies 3 --push-airtable
```

## Security

The following files and directories are excluded from version control:

* `.env`
* local virtual environments
* API debug responses
* generated logs
* raw, filtered, and enriched run data

Production credentials must be stored only in Railway environment variables.
