"""Unit tests for the filing request/response Pydantic models."""

import unittest

from pydantic import ValidationError

from app.filing.models import FilingRequest


def make(month="2026-11", lines=None):
    return FilingRequest.model_validate(
        {"month": month, "lines": lines or [{"line_id": "L1", "kilometres": 1}]}
    )


class FilingRequestModelTests(unittest.TestCase):
    def test_valid_request(self) -> None:
        req = make(lines=[{"line_id": "L1", "kilometres": 0},
                          {"line_id": "L2", "kilometres": 999}])
        self.assertEqual(req.content_map(), {"L1": 0, "L2": 999})

    def test_month_format(self) -> None:
        for bad in ("2026-13", "2026-00", "202611", "26-11", "2026-1", "abcd-ef"):
            with self.assertRaises(ValidationError):
                make(month=bad)
        for good in ("2026-01", "2026-12"):
            self.assertEqual(make(month=good).month, good)

    def test_line_count_bounds(self) -> None:
        # Empty list is rejected.
        with self.assertRaises(ValidationError):
            FilingRequest.model_validate({"month": "2026-11", "lines": []})
        # 21 lines exceed the maximum.
        with self.assertRaises(ValidationError):
            make(lines=[{"line_id": f"L{i}", "kilometres": 1} for i in range(21)])
        # exactly 20 is allowed
        req = make(lines=[{"line_id": f"L{i:02d}", "kilometres": 1}
                          for i in range(20)])
        self.assertEqual(len(req.lines), 20)

    def test_unique_line_ids(self) -> None:
        with self.assertRaises(ValidationError):
            make(lines=[{"line_id": "L1", "kilometres": 1},
                        {"line_id": "L1", "kilometres": 2}])

    def test_kilometres_non_negative_integer(self) -> None:
        with self.assertRaises(ValidationError):
            make(lines=[{"line_id": "L1", "kilometres": -1}])
        with self.assertRaises(ValidationError):
            make(lines=[{"line_id": "L1", "kilometres": 1.5}])

    def test_extra_fields_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            FilingRequest.model_validate(
                {"month": "2026-11",
                 "lines": [{"line_id": "L1", "kilometres": 1}],
                 "unexpected": True}
            )

    def test_blank_line_id_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            make(lines=[{"line_id": "  ", "kilometres": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
