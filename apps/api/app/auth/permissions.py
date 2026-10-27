from uuid import UUID


class MissingPermission(Exception):
    pass


def require_permission(principal: dict, permission: str, *, organization_id: UUID | str | None = None):
    """Enforce registered API-token scopes and keep tokens inside their owning organization."""
    if principal.get("credential_type") != "api_token":
        return
    if permission not in principal.get("scopes", ()):
        raise MissingPermission
    token_org = str(principal.get("organization_id", ""))
    if organization_id is None or str(organization_id) != token_org:
        raise MissingPermission
