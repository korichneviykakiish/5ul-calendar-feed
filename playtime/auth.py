"""One explicit TOTP login on the sampling runner, with no authentication retries."""

import requests

from session_state import user_agent
from .vrchat import ApiFailure, Client


def login_client(spacing):
    # Reuse the calendar's tested login flow, but never its other runner's cookies.
    import fetch_events as auth

    required = (auth.USERNAME, auth.PASSWORD, auth.TOTP_SECRET, auth.CONTACT, auth.SESSION_KEY)
    if not all(required):
        raise ApiFailure("configuration_error", operation="login", reason="missing-login-secrets")
    try:
        auth.session_cipher(auth.SESSION_KEY)
        auth.pyotp.TOTP(auth.TOTP_SECRET.replace(" ", "")).now()
    except (ValueError, TypeError):
        raise ApiFailure("configuration_error", operation="login", reason="invalid-login-secrets") from None

    session = requests.Session()
    session.headers["User-Agent"] = user_agent(auth.CONTACT)
    try:
        if not auth.login(session, "auto"):
            raise ApiFailure("login_required", operation="login", reason="email-confirmation-required")
    except auth.LoginFailure as error:
        raise ApiFailure(error.status, error.retry_after, operation="login", reason=error.reason) from None
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else 0
        kind = "access_denied" if status == 403 else "api_error"
        raise ApiFailure(kind, operation="login", http_status=status) from None
    except requests.RequestException:
        raise ApiFailure("network_error", operation="login") from None
    except ValueError:
        raise ApiFailure("invalid_response", operation="login") from None

    print("[playtime] Login confirmed on sampling runner")
    # Keep the authenticated HTTP session, including cookie domains and paths.
    return Client({}, auth.CONTACT, spacing, session=session)
