"""Product bindings and consistent source/temporal cuts (section 12).

Each authorized output port is bound to its last complete publication that is
available at submission and whose knowledge-complete watermark covers the
cutoff. Reads go through the port contract surface via the product's
snapshot adapter; the compiler never unwraps a port into base tables.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from .canonical import fingerprint, parse_ts, to_utc_z


@dataclass
class CutEntry:
    product_id: str
    contract_version: str
    port_id: str
    publication_id: str
    seq: int
    snapshot_at: str
    knowledge_complete_through: str
    replayable_until: str
    retention_checked_at: str


@dataclass
class SourceCut:
    cut_mode: str  # pinned | latest_provisional
    entries: list[CutEntry]
    admitted: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint({"cut_mode": self.cut_mode, "entries": [
            {k: v for k, v in asdict(e).items() if k not in ("retention_checked_at",)} for e in self.entries]})

    @property
    def seq_by_port(self) -> dict[str, int]:
        return {f"{e.product_id}/{e.port_id}": e.seq for e in self.entries}

    @property
    def replayable_until(self) -> str | None:
        return min((e.replayable_until for e in self.entries), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {"cut_mode": self.cut_mode, "admitted": self.admitted, "reasons": self.reasons,
                "fingerprint": self.fingerprint, "replayable_until": self.replayable_until,
                "entries": [asdict(e) for e in self.entries]}


def bind_ports(registry, recipe: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = []
    for ref in recipe["input_ports"]:
        port = registry.port(ref)
        bindings.append({"port_ref": ref, "product_id": port["product_id"], "port_id": port["port_id"],
                         "contract_version": port["contract_version"], "grain": port["grain"], "authority": port["authority"],
                         "snapshot_adapter": port["snapshot_adapter"], "row_access_policy": port["row_access_policy"]})
    return bindings


def select_cut(engine, registry, bindings: list[dict[str, Any]], knowledge_cutoff: str, mode: str, now: str) -> SourceCut:
    entries, reasons = [], []
    admitted = True
    cut_mode = "pinned" if mode == "pinned" else "latest_provisional"
    for b in bindings:
        pubs = engine.admin_query(
            "SELECT * FROM product_publications WHERE product_id=? AND port_id=? AND contract_version=? ORDER BY seq",
            (b["product_id"], b["port_id"], b["contract_version"]))
        available = [p for p in pubs if p["complete"] == 1 and p["snapshot_at"] <= now]
        if mode == "pinned":
            covering = [p for p in available if p["knowledge_complete_through"] >= knowledge_cutoff]
            chosen = covering[-1] if covering else None
            if chosen is None:
                admitted = False
                latest = available[-1] if available else None
                reasons.append(
                    f"{b['port_ref']}: no complete publication covers cutoff {knowledge_cutoff}"
                    + (f" (latest watermark {latest['knowledge_complete_through']})" if latest else ""))
                chosen = latest
        else:
            chosen = available[-1] if available else None
            if chosen and chosen["knowledge_complete_through"] < knowledge_cutoff:
                reasons.append(f"{b['port_ref']}: provisional — watermark {chosen['knowledge_complete_through']} precedes cutoff")
        if chosen is None:
            admitted = False
            reasons.append(f"{b['port_ref']}: no available publication")
            continue
        retention = registry.port(b["port_ref"])["replay_retention_hours"]
        entries.append(CutEntry(
            product_id=b["product_id"], contract_version=b["contract_version"], port_id=b["port_id"],
            publication_id=chosen["publication_id"], seq=int(chosen["seq"]), snapshot_at=chosen["snapshot_at"],
            knowledge_complete_through=chosen["knowledge_complete_through"],
            replayable_until=to_utc_z(parse_ts(chosen["snapshot_at"]) + timedelta(hours=retention)),
            retention_checked_at=now))
    return SourceCut(cut_mode=cut_mode, entries=entries, admitted=admitted, reasons=reasons)


def temporal_admission(cut: SourceCut, knowledge_cutoff: str, now: str) -> tuple[bool, list[str]]:
    reasons = []
    if knowledge_cutoff > now:
        reasons.append(f"cutoff {knowledge_cutoff} is in the future relative to {now}")
    for e in cut.entries:
        if cut.cut_mode == "pinned" and knowledge_cutoff > e.knowledge_complete_through:
            reasons.append(f"{e.product_id}/{e.port_id}: cutoff after knowledge-complete watermark {e.knowledge_complete_through}")
        if e.snapshot_at > now:
            reasons.append(f"{e.publication_id} not yet available")
    ok = not reasons and cut.admitted
    return ok, reasons + ([] if cut.admitted else cut.reasons)
