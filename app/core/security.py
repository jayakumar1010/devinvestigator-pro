import hashlib
import hmac


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate GitHub's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
