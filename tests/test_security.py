import unittest

from security import find_suspected_secrets


class TestSecurity(unittest.TestCase):
    def test_aws_key_detected(self):
        hits = find_suspected_secrets('aws_key = "AKIAABCDEFGHIJKLMNOP"')
        self.assertTrue(any("AKIA" in h for h in hits))

    def test_pem_header_detected(self):
        hits = find_suspected_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        self.assertTrue(any("PRIVATE KEY" in h for h in hits))

    def test_openai_style_key_detected(self):
        hits = find_suspected_secrets('token = "sk-abcdefghijklmnopqrstuvwx"')
        self.assertTrue(hits)

    def test_github_token_detected(self):
        hits = find_suspected_secrets("ghp_abcdefghijklmnopqrstuvwxyz012345")
        self.assertTrue(hits)

    def test_ordinary_code_not_flagged(self):
        code = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
        self.assertEqual(find_suspected_secrets(code), [])


if __name__ == "__main__":
    unittest.main()
