import unittest
from parse_change_log import parse_changelog  # Adjust import based on your module structure

class TestParseChangelog(unittest.TestCase):
    def test_empty_input(self):
        """Test that an empty string returns an empty list."""
        result = parse_changelog("")
        self.assertEqual(result, [])

    def test_simple_entry(self):
        """Test parsing a single simple changelog entry."""
        input_text = "Added feature X"
        # Assuming the parser splits by newlines and strips whitespace.
        expected = ["Added feature X"] 
        result = parse_changelog(input_text)
        self.assertEqual(result, expected)

    def test_multiple_entries(self):
        """Test parsing multiple entries separated by newlines."""
        input_text = "Fixed bug A\nAdded feature B\nRemoved old code"
        expected = [
            "Fixed bug A",
            "Added feature B",
            "Removed old code"
        ]
        result = parse_changelog(input_text)
        self.assertEqual(result, expected)

    def test_entries_with_extra_whitespace(self):
        """Test that extra whitespace around entries is handled."""
        input_text = "  Entry one  \n\nEntry two   "
        # Depending on implementation, this might return ["Entry one", "Entry two"] 
        # or keep the whitespace. Adjust expected based on your logic.
        result = parse_changelog(input_text)
        self.assertEqual(len(result), 2)

    def test_empty_lines_ignored(self):
        """Test that empty lines in the input are ignored."""
        input_text = "Line one\n\n\nLine two"
        expected = ["Line one", "Line two"]
        result = parse_changelog(input_text)
        self.assertEqual(result, expected)

    def test_case_sensitivity(self):
        """Test that parsing preserves original casing unless specified otherwise."""
        input_text = "UPPERCASE\nlowercase"
        result = parse_changelog(input_text)
        # Assuming default behavior is to preserve case
        self.assertEqual(result[0], "UPPERCASE")
        self.assertEqual(result[1], "lowercase")

if __name__ == '__main__':
    unittest.main()
