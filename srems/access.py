"""Credential lifecycle and role/floor authorization."""

from datetime import datetime

from .config import SREMSConfig
from .models import Credential, CredentialStatus, Role, utc_now


class AccessControl:
    def __init__(self, config: SREMSConfig) -> None:
        self.config = config
        self.credentials: dict[str, Credential] = {}

    def register(self, credential: Credential) -> None:
        if credential.credential_id in self.credentials:
            raise ValueError(f"Credential {credential.credential_id!r} already exists.")
        if any(not self.config.validate_floor(floor) for floor in credential.allowed_floors):
            raise ValueError("Credential includes a floor outside the configured range.")
        self.credentials[credential.credential_id] = credential

    def get_active_admin(
        self, credential_id: str | None, now: datetime | None = None
    ) -> Credential | None:
        if not credential_id:
            return None
        credential = self.credentials.get(credential_id)
        if credential is None or credential.role != Role.ADMIN:
            return None
        if self.status_at(credential, now or utc_now()) != CredentialStatus.ACTIVE:
            return None
        return credential

    @staticmethod
    def status_at(credential: Credential, now: datetime) -> CredentialStatus:
        if (
            credential.status == CredentialStatus.ACTIVE
            and credential.expires_at is not None
            and credential.expires_at <= now
        ):
            return CredentialStatus.EXPIRED
        return credential.status

    def authorize_destination(
        self,
        credential_id: str | None,
        destination: int,
        now: datetime,
    ) -> tuple[bool, str, Credential | None]:
        if destination == -1:
            return True, "B1 is available without a credential.", None
        if credential_id is None:
            return False, "A credential is required for this destination.", None
        credential = self.credentials.get(credential_id)
        if credential is None:
            return False, "Credential is not registered.", None
        status = self.status_at(credential, now)
        if status != CredentialStatus.ACTIVE:
            return False, f"Credential is {status.value.lower()}.", credential
        if destination not in credential.allowed_floors:
            return False, "Credential does not permit this destination.", credential
        return True, "Destination authorized.", credential

    def has_role(
        self, credential_id: str | None, roles: set[Role], now: datetime | None = None
    ) -> bool:
        current_time = now or utc_now()
        credential = self.get_active_admin(credential_id, current_time)
        if credential is not None:
            return Role.ADMIN in roles
        if not credential_id:
            return False
        credential = self.credentials.get(credential_id)
        if credential is None:
            return False
        return (
            self.status_at(credential, current_time) == CredentialStatus.ACTIVE
            and credential.role in roles
        )
