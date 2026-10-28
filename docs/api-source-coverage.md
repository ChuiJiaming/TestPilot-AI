# M01–M03 API source coverage snapshot

Date: 2026-10-28

The read-only inventory compared the Phase 1 OpenAPI operation IDs with literal FastAPI route decorators in the API source. It found **39/39 exact method, path, and operationId matches**. `create_app` also imports and includes the corresponding route factories in source; runtime app import and route registration were not checked.

| Source module | Matched operations |
|---|---:|
| `apps/api/app/auth/routes.py` | 10 |
| `apps/api/app/auth/lifecycle_routes.py` | 13 |
| `apps/api/app/tenancy/routes.py` | 10 |
| `apps/api/app/environments/routes.py` | 5 |
| `apps/api/app/main.py` (readiness) | 1 |
| **Total** | **39** |

| Method | Path | operationId | Source |
|---|---|---|---|
| POST | `/api/v1/auth/register` | `post__api_v1_auth_register` | `auth/routes.py` |
| POST | `/api/v1/auth/verify-email` | `post__api_v1_auth_verify_email` | `auth/routes.py` |
| POST | `/api/v1/auth/verification/resend` | `post__api_v1_auth_verification_resend` | `auth/routes.py` |
| POST | `/api/v1/auth/login` | `post__api_v1_auth_login` | `auth/routes.py` |
| POST | `/api/v1/auth/refresh` | `post__api_v1_auth_refresh` | `auth/routes.py` |
| POST | `/api/v1/auth/logout` | `post__api_v1_auth_logout` | `auth/routes.py` |
| GET | `/api/v1/auth/sessions` | `get__api_v1_auth_sessions` | `auth/routes.py` |
| DELETE | `/api/v1/auth/sessions/{session_id}` | `delete__api_v1_auth_sessions__session_id` | `auth/routes.py` |
| GET | `/api/v1/admin/verification-email-outbox/dead` | `get_api_v1_admin_verification_email_outbox_dead` | `auth/routes.py` |
| GET | `/api/v1/me` | `get__api_v1_me` | `auth/routes.py` |
| POST | `/api/v1/organizations/{org_id}/invitations` | `post__api_v1_organizations__org_id__invitations` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/invitations/accept` | `post__api_v1_invitations_accept` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/organizations/{org_id}/api-tokens` | `post__api_v1_organizations__org_id__api_tokens` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/api-tokens` | `post__api_v1_api_tokens` | `auth/lifecycle_routes.py` |
| GET | `/api/v1/api-tokens` | `get__api_v1_api_tokens` | `auth/lifecycle_routes.py` |
| DELETE | `/api/v1/api-tokens/{id}` | `delete__api_v1_api_tokens__id` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/api-tokens/{id}/rotate` | `post__api_v1_api_tokens__id__rotate` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/organizations/{org_id}/secret-refs` | `post__api_v1_organizations__org_id__secret_refs` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/secret-refs` | `post__api_v1_secret_refs` | `auth/lifecycle_routes.py` |
| GET | `/api/v1/secret-refs` | `get__api_v1_secret_refs` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/organizations/{org_id}/secret-refs/{secret_id}/rotate` | `post_api_v1_organizations_org_id_secret_refs_secret_id_rotate` | `auth/lifecycle_routes.py` |
| POST | `/api/v1/secret-refs/{id}/rotate` | `post__api_v1_secret_refs__id__rotate` | `auth/lifecycle_routes.py` |
| DELETE | `/api/v1/secret-refs/{id}` | `delete__api_v1_secret_refs__id` | `auth/lifecycle_routes.py` |
| GET | `/api/v1/organizations` | `get__api_v1_organizations` | `tenancy/routes.py` |
| POST | `/api/v1/organizations` | `post__api_v1_organizations` | `tenancy/routes.py` |
| GET | `/api/v1/organizations/{org_id}/members` | `get__api_v1_organizations__org_id__members` | `tenancy/routes.py` |
| GET | `/api/v1/projects` | `get__api_v1_projects` | `tenancy/routes.py` |
| POST | `/api/v1/projects` | `post__api_v1_projects` | `tenancy/routes.py` |
| GET | `/api/v1/projects/{id}` | `get__api_v1_projects__id` | `tenancy/routes.py` |
| PATCH | `/api/v1/projects/{id}` | `patch__api_v1_projects__id` | `tenancy/routes.py` |
| GET | `/api/v1/projects/{id}/members` | `get__api_v1_projects__id__members` | `tenancy/routes.py` |
| PUT | `/api/v1/projects/{id}/members/{user_id}` | `put__api_v1_projects__id__members__user_id` | `tenancy/routes.py` |
| DELETE | `/api/v1/projects/{project_id}/members/{user_id}` | `delete_api_v1_projects_project_id_members_user_id` | `tenancy/routes.py` |
| POST | `/api/v1/projects/{id}/environments` | `post__api_v1_projects__id__environments` | `environments/routes.py` |
| GET | `/api/v1/projects/{id}/environments` | `get__api_v1_projects__id__environments` | `environments/routes.py` |
| PATCH | `/api/v1/environments/{id}` | `patch__api_v1_environments__id` | `environments/routes.py` |
| GET | `/api/v1/environments/{id}/snapshots` | `get_api_v1_environments_id_snapshots` | `environments/routes.py` |
| POST | `/api/v1/environments/{id}/health-check` | `post__api_v1_environments__id__health_check` | `environments/routes.py` |
| GET | `/api/v1/health/ready` | `get_api_v1_health_ready` | `main.py` |

The inventory found the verification-email dead-letter handler under the wrong route family; it now matches `/api/v1/admin/verification-email-outbox/dead`, requires a platform-admin session, and returns the keyset page shape `{items,next_cursor}`. The readiness handler now declares its OpenAPI operation ID. The canonical generation POST's OpenAPI response was also corrected to 202 to match its source decorator; this M04 correction is outside the 39-operation Phase 1 count.

This is not runtime or conformance coverage. It does not establish response DTOs, permissions, tenant scope, error codes, app import/route registration, or behavior. `docs/api-runtime-coverage.md` is a stale 31/101 snapshot and must be regenerated during unified validation against the configured application.

No tests, app import, migration execution/validation, build, browser flow, or deployment check was performed for this inventory.
