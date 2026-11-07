import json
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

PROVISION_PATH = Path(__file__).with_name("provision-oncall-alerts.py")
SPEC = importlib.util.spec_from_file_location("provision_oncall_alerts", PROVISION_PATH)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ProvisionOncallAlertsTests(unittest.TestCase):
    def test_reads_exact_bootstrap_setting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / ".dev.vars"
            config.write_text("OTHER=value\nHUB2_BOOTSTRAP_TOKEN=" + "x" * 64 + "\n", encoding="utf-8")
            self.assertEqual(provision.bootstrap_token(config), "x" * 64)

    def test_encrypts_token_bundle_for_recipient(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            private_key = Path(temp_dir) / "recipient.key"
            public_key = Path(temp_dir) / "recipient.pub"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tokens = {"ingest_token": "1" * provision.TOKEN_SIZE, "poll_token": "2" * provision.TOKEN_SIZE}
            encrypted = provision.encrypt_for_recipient(
                json.dumps(tokens, separators=(",", ":")).encode("ascii"), public_key.read_bytes()
            )
            ciphertext = Path(temp_dir) / "bundle.enc"
            ciphertext.write_bytes(encrypted)
            decrypted = subprocess.run(
                [
                    "openssl", "pkeyutl", "-decrypt", "-inkey", str(private_key), "-in", str(ciphertext),
                    "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                    "-pkeyopt", "rsa_mgf1_md:sha256",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(json.loads(decrypted.stdout), tokens)

    def test_provisioning_request_returns_only_token_fields(self):
        bootstrap = "b" * 64
        response = Mock(status=201)
        response.read.return_value = json.dumps(
            {
                "configured": True,
                "ingest_token": "1" * provision.TOKEN_SIZE,
                "poll_token": "2" * provision.TOKEN_SIZE,
                "ingest_path": "/webhooks/oncall",
            }
        ).encode("ascii")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)

        bundle = provision.token_bundle(bootstrap, opener)

        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, provision.CONFIG_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {bootstrap}")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(json.loads(bundle), {"ingest_token": "1" * provision.TOKEN_SIZE, "poll_token": "2" * provision.TOKEN_SIZE})

    def test_rotation_is_explicit(self):
        response = Mock(status=201)
        response.read.return_value = json.dumps(
            {"configured": True, "ingest_token": "1" * provision.TOKEN_SIZE, "poll_token": "2" * provision.TOKEN_SIZE}
        ).encode("ascii")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)

        provision.token_bundle("b" * 64, opener, rotate=True)

        self.assertEqual(opener.call_args.args[0].data, b'{"rotate":true}')


if __name__ == "__main__":
    unittest.main()
