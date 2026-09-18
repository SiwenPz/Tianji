"""Import bootstrap so adapter modules can use the shared Tianji contracts.

The skill ships as a flat ``scripts/`` directory whose modules import each
other by plain module name, so the adapter mirrors that contract instead of
inventing a second import style.
"""
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from role_contract import (  # noqa: E402
    CANONICAL_CAPABILITIES,
    PRIMARY_BINDING,
    ROLE_TIERS,
    RolePackage,
    RoleRenderer,
    all_roles,
    canonical_capabilities,
    discover_role_packages,
    load_role_package,
    roles_for_tier,
    tier_for_role,
    validate_role_tiers,
)
from ledger_schema import (  # noqa: E402
    EventKey,
    build_event,
    event_key_from,
    normalize_legacy_event,
    require_canonical_uuid,
    require_timestamp,
    validate_event,
)

__all__ = [
    "CANONICAL_CAPABILITIES",
    "PRIMARY_BINDING",
    "ROLE_TIERS",
    "RolePackage",
    "RoleRenderer",
    "all_roles",
    "canonical_capabilities",
    "discover_role_packages",
    "load_role_package",
    "roles_for_tier",
    "tier_for_role",
    "validate_role_tiers",
    "EventKey",
    "build_event",
    "event_key_from",
    "normalize_legacy_event",
    "require_canonical_uuid",
    "require_timestamp",
    "validate_event",
]
