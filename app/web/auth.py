import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config import Settings, get_settings

_basic = HTTPBasic(auto_error=False, realm="DevInvestigator")


def require_dashboard_user(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """HTTP Basic auth for the web page. The page stays off until DASHBOARD_PASSWORD is set."""
    if settings.dashboard_password is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The web page is disabled: set DASHBOARD_PASSWORD to enable it")
    if credentials is not None:
        # Compare both, always, in constant time.
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.dashboard_username.encode())
        password_ok = secrets.compare_digest(
            credentials.password.encode(), settings.dashboard_password.get_secret_value().encode()
        )
        if user_ok and password_ok:
            return credentials.username
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="DevInvestigator"'}
    )
