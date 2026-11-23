# Cube configuration in Python (Cube also accepts cube.js).
#
# Authentication is configured with env vars, not here: Cube verifies every request's
# JWT against OpenBao's JWKS (CUBEJS_JWK_URL), and checks issuer and audience. The
# verified claims become the "security context". This file maps them to groups for
# the access policies in model/views/fleet.yml.
from cube import config


@config("context_to_groups")
def context_to_groups(ctx: dict) -> list[str]:
    # OpenBao's identity token carries the entity's group names as a list claim.
    return ctx.get("securityContext", {}).get("groups") or []


@config("scheduled_refresh_contexts")
def scheduled_refresh_contexts() -> list[dict]:
    # The background refresher builds pre-aggregations as an admin identity.
    return [{"securityContext": {"team": "platform", "groups": ["sl-admins"]}}]
