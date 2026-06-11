import unittest
from parse_change_log import parse_log

class TestParseChangeLog(unittest.TestCase):
    def test_parse_log_with_valid_input(self):
        log = """
        1.0.0 (2023-04-01)
        - Added new feature X
        - Fixed bug Y
        """
        expected_output = {
            "version": "1.0.0",
            "date": "2023-04-01",
            "changes": [
                "Added new feature X",
                "Fixed bug Y"
            ]
        }
        self.assertEqual(parse_log(log), expected_output)

    def test_parse_log_with_empty_input(self):
        log = ""
        expected_output = {
            "version": None,
            "date": None,
            "changes": []
        }
        self.assertEqual(parse_log(log), expected_output)

    def test_parse_log_with_no_changes(self):
        log = """
        1.0.0 (2023-04-01)
        """
        expected_output = {
            "version": "1.0.0",
            "date": "2023-04-01",
            "changes": []
        }
        self.assertEqual(parse_log(log), expected_output)

if __name__ == '__main__':
    unittest.main()
