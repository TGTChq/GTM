"""Shared source-platform identities; these are not employer domains.

Use one registry for job URL recognition and employer-identity rejection so a
recognized ATS cannot merge independent employer tenants during enrichment.
"""

ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "workdayjobs.com", "icims.com", "smartrecruiters.com", "jobvite.com",
    "breezy.hr", "workable.com", "recruitee.com", "applytojob.com",
    "adp.com", "oraclecloud.com", "successfactors.com", "bamboohr.com",
    "personio.com", "rippling.com", "eightfold.ai", "phenompeople.com",
    "teamtailor.com", "careers-page.com", "comeet.co", "pinpointhq.com",
    "paylocity.com", "dayforcehcm.com", "ultipro.com", "ukg.com",
    "jobsoid.com", "jazz.co", "clearcompany.com", "applicantpro.com",
    "hiringthing.com", "isolvedhire.com", "avature.net", "csod.com",
    "taleo.net", "paycomonline.net", "paycomonline.com", "myworkdaysite.com",
}
