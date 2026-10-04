"""Fixtures are made up. Run: python3 tests/test_secretscan.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gutcheck", "src"))
from gutcheck_core import secretscan as s

POS = ["here is my key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
       "export OPENAI_API_KEY=sk-proj-Zx9aB8cD7eF6gH5iJ4kL3mN2oP1qR0sT",
       "apik_3f9Kq2LmZx8Rt5Vn7Wb1Yc4", "vck_9aZ3kLq8Xv2Rt7Mn4Pb6Wc1Yd",
       "ghp_ABCdefGHIjklMNOpqrSTUvwxYZ0123456789ab", "AKIAIOSFODNN7EXAMPLE",
       "token: 'Zk3Lm9Qp2Xv8Rt5Wn7Bc1Yd4Fg6Hj0Ks'", "q8Zr3LmX9vKt2WnB7pYc5Fd1Gh6Js4Qa0Ue8Ri3To",
       "-----BEGIN OPENSSH PRIVATE KEY-----"]
NEG = ["commit 3f9a1c2e4b5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f fixed the bug",
       "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
       "/Users/someone/projects/app/src/components/VeryLongComponentNameHere.tsx",
       "please refactor the authentication_token_refresh_handler function",
       "uuid 123e4567-e89b-12d3-a456-426614174000", "set SESSION_TIMEOUT=3600 and restart",
       "base64 of hello is aGVsbG8gd29ybGQ=", "Responsibilities: the design doc and the spec",
       "password_reset_email_template_v2_final_version"]
for t in POS:
    assert s.find(t), t
    assert "REDACTED" in s.redact(t)
for t in NEG:
    assert not s.find(t), (t, s.find(t))
for f, want in [(".env", 1), (".env.local", 1), (".env.example", 0), ("a/server.pem", 1), ("id_rsa", 1),
                ("id_rsa.pub", 0), ("x/credentials.json", 1), ("README.md", 0), ("~/.aws/credentials", 1),
                ("keys.ts", 0), ("api.key", 1)]:
    assert s.is_secret_file(f) == bool(want), f
print("secretscan ok")

# ---- prompt policy: block only known key formats; URLs never count unless a query value is a key ----
NO_BLOCK = [
    "grab the pack at https://store.example-assets.com/packs/polygon-fantasy-hero-characters-f8Kq2LmZx9Rt5Vn7Wb1Yc4Dq3Gh6Jk0/download?ref=Zp4Qr8Lm2Xv9Kt5Wn7Bc1Yd",
    "see https://s3.amazonaws.com/bucket/obj.zip?X-Amz-Algorithm=AWS4&X-Amz-Signature=9f8e7d6c5b4a39281706f5e4d3c2b1a0aa55bb66cc77dd88ee99ff0011223344",
    "commit https://github.com/example/repo/commit/3f9a1c2e4b5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f and https://docs.google.com/document/d/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV2wX3yZ4aB5c/edit",
    "cdn https://cdn.example.net/assets/Zq8Lm3Xv9Kt2Wn7Bc5Yd1Fg6Hj0Ks4Qa8Ue3Ri7To/app.js and https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "path url https://example.com/sk-proj-looks-like-but-is-a-slug-abcdefghij/page",
]
BLOCK = [
    "use https://api.example.com/v1/items?api_key=sk-proj-Zx9aB8cD7eF6gH5iJ4kL3mN2oP1qR0sT",
    "my key is ghp_ABCdefGHIjklMNOpqrSTUvwxYZ0123456789ab",
    "AKIAIOSFODNN7EXAMPLE is the access key",
    "vck_9aZ3kLq8Xv2Rt7Mn4Pb6Wc1Yd and apik_3f9Kq2LmZx8Rt5Vn7Wb1Yc4",
]
WARN_ONLY = ["token: 'Zk3Lm9Qp2Xv8Rt5Wn7Bc1Yd4Fg6Hj0Ks'", "q8Zr3LmX9vKt2WnB7pYc5Fd1Gh6Js4Qa0Ue8Ri3To"]
for t in NO_BLOCK:
    b, w = s.classify_prompt(t)
    assert not b and not w, (t, b, w)
for t in BLOCK:
    b, w = s.classify_prompt(t)
    assert b, (t, b, w)
for t in WARN_ONLY:
    b, w = s.classify_prompt(t)
    assert not b and w, (t, b, w)
print("prompt policy ok")

# templates stay readable, whatever the middle part of the name
for f in [".env.example", ".env.local.example", ".env.production.template", ".env.default", ".env.development.default",
          ".env.light.example", ".env.do.example", ".env.docker.example", ".env.sample"]:
    assert not s.is_secret_file(f), f
for f in [".env", ".env.local", ".env.staging-r2", ".env.production", ".env.testnet3", ".env.vault", "id_rsa"]:
    assert s.is_secret_file(f), f
print("env templates ok")
