import tempfile
import unittest
from pathlib import Path

import textfile


class TestDetectEol(unittest.TestCase):
    def test_pure_lf(self):
        self.assertEqual(textfile.detect_eol("a\nb\nc\n"), ("\n", False))

    def test_pure_crlf(self):
        self.assertEqual(textfile.detect_eol("a\r\nb\r\nc\r\n"), ("\r\n", False))

    def test_mixed_crlf_dominant(self):
        eol, mixed = textfile.detect_eol("a\r\nb\r\nc\n")
        self.assertEqual(eol, "\r\n")
        self.assertTrue(mixed)

    def test_mixed_lf_dominant(self):
        eol, mixed = textfile.detect_eol("a\nb\nc\r\n")
        self.assertEqual(eol, "\n")
        self.assertTrue(mixed)

    def test_tie_is_not_a_strict_crlf_majority(self):
        eol, mixed = textfile.detect_eol("a\r\nb\n")
        self.assertEqual(eol, "\n")
        self.assertTrue(mixed)

    def test_empty_and_single_line(self):
        self.assertEqual(textfile.detect_eol(""), ("\n", False))
        self.assertEqual(textfile.detect_eol("no newline here"), ("\n", False))


class TestReadWriteRoundTrip(unittest.TestCase):
    def test_crlf_round_trip_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "crlf.txt")
            raw = b"one\r\ntwo\r\nthree\r\n"
            path.write_bytes(raw)
            parsed = textfile.read(path)
            self.assertEqual(parsed.eol, "\r\n")
            self.assertFalse(parsed.bom)
            self.assertFalse(parsed.mixed_eol)
            textfile.write(path, parsed.text, bom=parsed.bom)
            self.assertEqual(path.read_bytes(), raw)

    def test_bom_plus_crlf_round_trip_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "bom.txt")
            raw = b"\xef\xbb\xbfone\r\ntwo\r\n"
            path.write_bytes(raw)
            parsed = textfile.read(path)
            self.assertTrue(parsed.bom)
            self.assertEqual(parsed.text, "one\r\ntwo\r\n")   # BOM stripped from text
            textfile.write(path, parsed.text, bom=parsed.bom)
            self.assertEqual(path.read_bytes(), raw)

    def test_read_raises_on_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "latin1.txt")
            path.write_bytes(b"caf\xe9\n")
            with self.assertRaises(UnicodeDecodeError):
                textfile.read(path)

    def test_write_with_unpaired_surrogate_does_not_truncate_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "keep.txt")
            original = b"original content\n"
            path.write_bytes(original)
            with self.assertRaises(UnicodeEncodeError):
                textfile.write(path, "bad \ud800\n", bom=False)
            self.assertEqual(path.read_bytes(), original)


class TestToEol(unittest.TestCase):
    def test_renders_lf_source_as_crlf(self):
        self.assertEqual(textfile.to_eol("a\nb\n", "\r\n"), "a\r\nb\r\n")

    def test_renders_crlf_source_as_lf(self):
        self.assertEqual(textfile.to_eol("a\r\nb\r\n", "\n"), "a\nb\n")

    def test_is_idempotent(self):
        self.assertEqual(textfile.to_eol(textfile.to_eol("a\nb\n", "\r\n"), "\r\n"), "a\r\nb\r\n")


if __name__ == "__main__":
    unittest.main()
