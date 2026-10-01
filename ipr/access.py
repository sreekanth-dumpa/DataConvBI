"""ADR-01: requester identity, effective access and reuse authorization.

Analytical reads use a delegated requester session (simulated in the local
engine). The control plane never impersonates the requester and never falls
back to a broader service principal when a delegation expires.

The access fingerprint covers the subject, purpose, tenant, policy dependency
versions, entitlement epoch and effective row visibility. Shared reuse keys on
an effective-access *class* only when the provider certifies identical row
visibility, masks, purpose/tenant restrictions and policy dependencies;
disclosure labels (OPEN/TOKENIZED/FULL) are never sufficient.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .canonical import fingerprint
from .intent import Requester


@dataclass
class AccessDecision:
    subject: str
    tenant: str
    purpose: str
    snowflake_user: str
    role_context: dict[str, Any]
    effective_states: list[str]
    policy_version: str
    entitlement_epoch: str
    policy_dependencies: list[str]
    disclosure_label: str
    fingerprint: str
    access_class: str | None
    class_certificate_ref: str | None
    reuse_mode: str  # subject_scoped | certified_class
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def reuse_key(self) -> str:
        return self.access_class if self.reuse_mode == "certified_class" and self.access_class else self.fingerprint


class AccessProvider:
    """Reads the current (query-time) policy mapping from the engine's catalog."""

    def __init__(self, engine, certify_classes: bool = False):
        self.engine = engine
        self.certify_classes = certify_classes

    def _meta(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.engine.admin_query("SELECT key, value FROM policy_meta")}

    def establish(self, requester: Requester, port_refs: list[str]) -> AccessDecision:
        meta = self._meta()
        states = sorted(r["state"] for r in self.engine.admin_query(
            "SELECT DISTINCT state FROM policy_state_mapping WHERE subject=? AND purpose=?", (requester.subject, requester.purpose)))
        policy_deps = sorted({str(self.engine.registry.port(p).get("row_access_policy") or "unknown") for p in port_refs})
        reasons = []
        allowed = bool(states)
        if not states:
            reasons.append(f"subject {requester.subject} has no entitlement for purpose {requester.purpose}")
        if self.engine.token_expired(requester.subject):
            allowed = False
            reasons.append("delegated token expired; renew authorization (no service-principal fallback)")
        base = {"policy_version": meta.get("policy_version"), "entitlement_epoch": meta.get("entitlement_epoch"),
                "policy_dependencies": policy_deps, "effective_states": states, "purpose": requester.purpose,
                "tenant": requester.tenant, "masks": []}
        fp = fingerprint({"subject": requester.subject, **base})
        access_class = None
        cert = None
        mode = "subject_scoped"
        unknown_deps = any(d in (None, "", "unknown") for d in policy_deps) or not policy_deps
        if unknown_deps:
            reasons.append("unknown policy dependencies: cross-subject reuse forbidden")
        if self.certify_classes and states and not unknown_deps:
            # The provider certifies identical visibility for every subject in the class.
            access_class = fingerprint({"class": base})
            cert = f"access-class-cert/{access_class.split(':')[1][:12]}"
            mode = "certified_class"
        return AccessDecision(
            subject=requester.subject, tenant=requester.tenant, purpose=requester.purpose,
            snowflake_user=requester.subject.upper(), role_context={"primary": f"ROLE_{requester.purpose.upper()}", "secondary": []},
            effective_states=states, policy_version=meta.get("policy_version", "unknown"),
            entitlement_epoch=meta.get("entitlement_epoch", "unknown"), policy_dependencies=policy_deps,
            disclosure_label="TOKENIZED", fingerprint=fp, access_class=access_class, class_certificate_ref=cert,
            reuse_mode=mode, allowed=allowed, reasons=reasons)

    def reauthorize(self, requester: Requester, port_refs: list[str], recorded: dict[str, Any]) -> tuple[bool, str]:
        """Re-evaluate current access before a cached read or stage read."""
        now = self.establish(requester, port_refs)
        if not now.allowed:
            return False, "; ".join(now.reasons) or "not allowed"
        if recorded.get("access_reuse_mode") == "certified_class":
            if now.access_class and now.access_class == recorded.get("access_class"):
                return True, "certified access class unchanged"
            return False, "access class differs or is no longer certified"
        if now.fingerprint == recorded.get("access_fingerprint"):
            return True, "subject access fingerprint unchanged"
        return False, "effective access or policy dependencies changed since production"
