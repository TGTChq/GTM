from __future__ import annotations

import unittest

from fantastic_jobs_adapter import _employment_type, map_record


class FantasticEmploymentTypeTests(unittest.TestCase):
    def test_list_employment_type_takes_first_scalar(self):
        self.assertEqual(_employment_type(["FULL_TIME"]), "FULL_TIME")
        self.assertEqual(_employment_type(["FULL_TIME", "PART_TIME"]), "FULL_TIME")
        self.assertEqual(_employment_type([]), "FULLTIME")
        self.assertEqual(_employment_type("full_time"), "FULL_TIME")
        self.assertEqual(_employment_type(None), "FULLTIME")
        self.assertEqual(_employment_type(""), "FULLTIME")

    def test_map_record_never_serializes_a_list(self):
        rec = {"id": "2316770021", "title": "Credit Control Executive",
               "organization": "Oriental Sheet Piling", "employment_type": ["FULL_TIME"]}
        job, reason = map_record(rec, "fantastic_jobs_linkedin")
        self.assertEqual(reason, "")
        self.assertEqual(job["job_employment_type"], "FULL_TIME")
        self.assertNotIn("[", job["job_employment_type"])
        self.assertNotIn("'", job["job_employment_type"])

    def test_map_record_scalar_and_missing(self):
        job, _ = map_record({"id": "1", "title": "Engineer", "organization": "Acme",
                             "employment_type": "FULLTIME"}, "fantastic_jobs_linkedin")
        self.assertEqual(job["job_employment_type"], "FULLTIME")
        job2, _ = map_record({"id": "1", "title": "Engineer", "organization": "Acme"},
                             "fantastic_jobs_linkedin")
        self.assertEqual(job2["job_employment_type"], "FULLTIME")


if __name__ == "__main__":
    unittest.main()
