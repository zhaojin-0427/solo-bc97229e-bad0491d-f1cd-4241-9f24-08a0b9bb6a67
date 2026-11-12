"""CSV 解析规则测试。"""
import unittest

from elevation.errors import PayloadTooLarge, ValidationError
from elevation.parsing import parse_int_grid, render_window_csv
from tests.helpers import csv_stream, grid_csv


class ParseTest(unittest.TestCase):
    def parse(self, payload, max_rows=256, max_cols=256, max_bytes=1 << 20):
        return parse_int_grid(csv_stream(payload), max_rows, max_cols, max_bytes)[0]

    def test_basic_grid(self):
        grid = self.parse(b"1,2,3\n4,5,6\n")
        self.assertEqual(grid, [[1, 2, 3], [4, 5, 6]])

    def test_negative_and_zero(self):
        self.assertEqual(self.parse(b"-1,0,+2\n"), [[-1, 0, 2]])

    def test_empty_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"")
        with self.assertRaises(ValidationError):
            self.parse(b"   \n")

    def test_header_like_row_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"a,b\n1,2\n")

    def test_blank_cell_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"1,,3\n")

    def test_ragged_rows_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"1,2,3\n4,5\n")

    def test_blank_line_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"1,2\n\n3,4\n")

    def test_float_rejected(self):
        with self.assertRaises(ValidationError):
            self.parse(b"1.5,2\n")

    def test_size_limits(self):
        with self.assertRaises(ValidationError):
            self.parse(grid_csv(3, 2), max_rows=2)
        with self.assertRaises(ValidationError):
            self.parse(grid_csv(2, 3), max_cols=2)

    def test_byte_cap(self):
        with self.assertRaises(PayloadTooLarge):
            self.parse(grid_csv(10, 10), max_bytes=32)

    def test_int32_range(self):
        with self.assertRaises(ValidationError):
            self.parse(b"2147483648\n")
        with self.assertRaises(ValidationError):
            self.parse(b"-2147483649\n")
        self.assertEqual(self.parse(b"2147483647\n-2147483648\n"),
                         [[2147483647], [-2147483648]])

    def test_render_empty_cells(self):
        text = render_window_csv([[1, None], [None, 2]])
        self.assertEqual(text, "1,\n,2\n")


if __name__ == "__main__":
    unittest.main()
