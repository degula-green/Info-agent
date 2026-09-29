from __future__ import annotations

import unittest

from app.domain.state import (
    EXTERNAL_JOB_STATUS,
    JobStatus,
    validate_job_transition,
    validate_projection_transition,
)


class Phase1AStateTests(unittest.TestCase):
    def test_retry_wait_maps_to_external_processing(self) -> None:
        self.assertEqual(
            EXTERNAL_JOB_STATUS[JobStatus.RETRY_WAIT],
            "processing",
        )

    def test_illegal_job_transition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "illegal job transition"):
            validate_job_transition("ready", "processing")

    def test_illegal_projection_transition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "illegal projection transition"):
            validate_projection_transition("ready", "indexing")


if __name__ == "__main__":
    unittest.main()
