import unittest

from context.denylist import is_denied


class TestDenylist(unittest.TestCase):
    def test_env_family_denied(self):
        for path in (".env", ".env.local", ".env.production", ".ENV.LOCAL"):
            self.assertTrue(is_denied(path), path)

    def test_ssh_key_family_denied(self):
        for path in ("id_rsa", "id_rsa.pub", "id_ecdsa", "id_ed25519"):
            self.assertTrue(is_denied(path), path)

    def test_extensions_denied(self):
        for path in ("secret.pem", "cert.key", "bundle.p12", "store.jks"):
            self.assertTrue(is_denied(path), path)

    def test_exact_names_denied(self):
        for path in (".netrc", "credentials", "secrets.yaml"):
            self.assertTrue(is_denied(path), path)

    def test_ordinary_files_not_denied(self):
        for path in ("main.py", "README.md", "src/lib.rs", "notes.txt"):
            self.assertFalse(is_denied(path), path)


if __name__ == "__main__":
    unittest.main()
