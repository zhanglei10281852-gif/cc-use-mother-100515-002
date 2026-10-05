import unittest
from task_domain_002.core import Record, detect_conflicts, stable_summary

class ContractTests(unittest.TestCase):
    def test_digest_is_stable(self):
        record=Record("sample", "v1", "draft", "owner", (("kind", "demo"),))
        self.assertEqual(record.digest(), Record("sample", "v1", "draft", "owner", (("kind", "demo"),)).digest())

    def test_conflict_is_observable(self):
        rows=[Record("sample", "v1", "draft", "owner"), Record("sample", "v2", "approved", "owner")]
        self.assertEqual(detect_conflicts(rows), [("sample", "v1->v2")])

    def test_summary_has_public_shape(self):
        summary=stable_summary(Record("sample", "v1", "draft", "owner"))
        self.assertEqual(set(summary), {"code", "version", "state", "digest"})

if __name__ == "__main__": unittest.main()
