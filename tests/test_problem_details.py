import unittest
from uuid import UUID

from apps.api.app.core.errors import ProblemDetails, problem_details


class ProblemDetailsTests(unittest.TestCase):
    def test_problem_response_has_stable_fields_and_trace_id(self):
        error = problem_details(
            code="NOT_FOUND",
            message="Resource not found",
            trace_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        )

        self.assertEqual(
            error.model_dump(),
            {
                "code": "NOT_FOUND",
                "message": "Resource not found",
                "trace_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "details": None,
            },
        )

    def test_problem_details_do_not_require_exposing_sensitive_details(self):
        error = problem_details(
            code="INTERNAL_ERROR",
            message="An internal error occurred",
            trace_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )

        self.assertIsNone(error.details)
        self.assertNotIn("traceback", error.model_dump_json().lower())


if __name__ == "__main__":
    unittest.main()
