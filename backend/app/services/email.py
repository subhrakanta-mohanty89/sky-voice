"""Email sending via Mailgun's HTTPS API.

Uses only stdlib (``urllib`` + ``base64``) so we don't add a new dependency.
Mailgun's API accepts a simple ``application/x-www-form-urlencoded`` POST,
authenticated with HTTP Basic where the username is the literal string
``api`` and the password is the API key.
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# These are read at call time (not import time) so .env edits via
# `gcloud run services update` take effect on the next request without a
# redeploy.
_TIMEOUT_SECONDS = 10.0


class EmailError(RuntimeError):
    """Raised when Mailgun returns a non-2xx response or the call fails."""


def _config() -> dict[str, str]:
    return {
        "domain":   (os.getenv("MAILGUN_DOMAIN") or "").strip(),
        "api_key":  (os.getenv("MAILGUN_API_KEY") or "").strip(),
        "base_url": (os.getenv("MAILGUN_BASE_URL") or "https://api.mailgun.net/v3").rstrip("/"),
        "default_from": (os.getenv("MAIL_FROM") or "").strip(),
    }


def is_configured() -> bool:
    """True iff the env has the minimum needed to actually send."""
    cfg = _config()
    return bool(cfg["domain"] and cfg["api_key"] and cfg["default_from"])


def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    from_addr: Optional[str] = None,
) -> None:
    """Send a transactional email. Raises :class:`EmailError` on failure."""
    cfg = _config()
    if not is_configured():
        raise EmailError(
            "Mailgun is not configured — set MAILGUN_DOMAIN, MAILGUN_API_KEY and MAIL_FROM."
        )

    url = f"{cfg['base_url']}/{cfg['domain']}/messages"
    fields: list[tuple[str, str]] = [
        ("from", from_addr or cfg["default_from"]),
        ("to", to),
        ("subject", subject),
        ("text", text),
    ]
    if html:
        fields.append(("html", html))

    data = urllib.parse.urlencode(fields).encode("utf-8")
    auth = base64.b64encode(f"api:{cfg['api_key']}".encode("utf-8")).decode("ascii")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                logger.info("Mailgun: sent %r -> %s (HTTP %d)", subject, to, resp.status)
                return
            body = resp.read().decode("utf-8", errors="replace")
            raise EmailError(f"Mailgun returned HTTP {resp.status}: {body[:300]}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        raise EmailError(f"Mailgun HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise EmailError(f"Mailgun network error: {exc}") from exc


# --------------------------------------------------------------------------- #
#  Pre-built templates
# --------------------------------------------------------------------------- #

def send_signup_otp(to: str, *, code: str, full_name: str = "", ttl_minutes: int = 10) -> None:
    name = full_name.strip().split()[0] if full_name.strip() else "there"
    text = (
        f"Hi {name},\n\n"
        f"Your Sky Voice AI verification code is: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes. If you didn't request it, "
        "you can safely ignore this email.\n\n"
        "— Sky Voice AI"
    )
    html = f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f4f6fb;padding:24px;color:#0f172a;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:14px;padding:32px 28px;box-shadow:0 1px 4px rgba(15,23,42,.08);">
    <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#64748b;margin-bottom:6px;">Sky Voice AI</div>
    <h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;">Verify your email</h1>
    <p style="margin:0 0 20px;line-height:1.55;">Hi {name}, use this code to finish signing in:</p>
    <div style="font-size:34px;font-weight:700;letter-spacing:6px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:14px 18px;text-align:center;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{code}</div>
    <p style="margin:18px 0 0;font-size:13px;color:#64748b;line-height:1.55;">
      This code expires in {ttl_minutes} minutes. If you didn't request it, you can safely ignore this email.
    </p>
  </div>
</body></html>"""
    send_email(to=to, subject="Your Sky Voice AI verification code", text=text, html=html)
