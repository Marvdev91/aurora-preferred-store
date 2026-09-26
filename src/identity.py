"""
Identity resolution.

The problem in one sentence: Square knows a shopper by a half-typed email, mPOS
knows the same shopper by a loyalty card, SFCC knows them by an account email,
and no single system holds all three. A join on email loses roughly half the
in-store transactions in Aurora's data.

The approach: treat every identifier as a node, treat every transaction and CRM
record as evidence that its identifiers belong to the same person, and take the
connected components. This is a union-find (disjoint set) over identifier
tokens. It is deliberately simple, deterministic, auditable and cheap — three
properties that matter more than cleverness when a marketing team is going to be
asked to trust the output.

We then pick one canonical identifier set per cluster following Klaviyo's own
identifier priority: email is the primary identifier, external_id is the durable
cross-system key, phone is the channel key for the SMS migration that follows.

Known limitation, stated up front rather than discovered in UAT: connected
components merge aggressively. A shared household email plus a shared loyalty
card will collapse two people into one profile. Guardrails are in
`suspicious_clusters` and the mitigation is discussed in the one-pager.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .normalize import Transaction

# A cluster this large is almost certainly an over-merge (a staff card, a store
# email, a test account) rather than a real person. We quarantine rather than load.
MAX_IDENTIFIERS_PER_CLUSTER = 12


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:   # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class Identity:
    key: str                                   # the cluster root, internal only
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    loyalty_ids: set = field(default_factory=set)
    crm_ids: set = field(default_factory=set)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    country: Optional[str] = None

    @property
    def primary_email(self) -> Optional[str]:
        return sorted(self.emails)[0] if self.emails else None

    @property
    def primary_phone(self) -> Optional[str]:
        return sorted(self.phones)[0] if self.phones else None

    @property
    def external_id(self) -> Optional[str]:
        """
        Klaviyo's external_id should be the key Aurora's other systems agree on.
        CRM id first, loyalty number as the fallback — never an email, because
        emails change and external_id is supposed to survive that.
        """
        if self.crm_ids:
            return sorted(self.crm_ids)[0]
        if self.loyalty_ids:
            return sorted(self.loyalty_ids)[0]
        return None

    @property
    def identifier_count(self) -> int:
        return len(self.emails) + len(self.phones) + len(self.loyalty_ids) + len(self.crm_ids)

    @property
    def is_addressable(self) -> bool:
        """No email and no phone means nothing to send to — don't create a profile."""
        return bool(self.emails or self.phones)


def _tokens(email=None, phone=None, loyalty_id=None, crm_id=None) -> list[str]:
    """Namespaced tokens so an email can never collide with a loyalty number."""
    out = []
    if email:
        out.append(f"email:{email}")
    if phone:
        out.append(f"phone:{phone}")
    if loyalty_id:
        out.append(f"loyalty:{loyalty_id}")
    if crm_id:
        out.append(f"crm:{crm_id}")
    return out


def resolve(transactions: list[Transaction], crm_records: list[dict]) -> tuple[
        dict[str, Identity], dict[str, str], list[Identity]]:
    """
    Returns:
        identities:    cluster key -> Identity
        txn_to_key:    transaction id -> cluster key (only for identified txns)
        quarantined:   clusters held back for review rather than loaded
    """
    uf = UnionFind()

    # Pass 1 — CRM is the strongest evidence: it explicitly asserts that this
    # email, phone and loyalty card are the same person.
    for record in crm_records:
        tokens = _tokens(record.get("email"), record.get("phone"),
                         record.get("loyalty_id"), record.get("crm_id"))
        for token in tokens[1:]:
            uf.union(tokens[0], token)

    # Pass 2 — each transaction links whatever identifiers it carries together.
    for txn in transactions:
        tokens = _tokens(txn.email, txn.phone, txn.loyalty_id, txn.crm_id)
        for token in tokens[1:]:
            uf.union(tokens[0], token)

    # Build clusters.
    identities: dict[str, Identity] = {}

    def _ensure(token: str) -> Identity:
        root = uf.find(token)
        return identities.setdefault(root, Identity(key=root))

    for record in crm_records:
        tokens = _tokens(record.get("email"), record.get("phone"),
                         record.get("loyalty_id"), record.get("crm_id"))
        if not tokens:
            continue
        identity = _ensure(tokens[0])
        identity.emails.update(filter(None, [record.get("email")]))
        identity.phones.update(filter(None, [record.get("phone")]))
        identity.loyalty_ids.update(filter(None, [record.get("loyalty_id")]))
        identity.crm_ids.update(filter(None, [record.get("crm_id")]))
        identity.first_name = identity.first_name or record.get("first_name")
        identity.last_name = identity.last_name or record.get("last_name")
        identity.country = identity.country or record.get("country")

    txn_to_key: dict[str, str] = {}
    for txn in transactions:
        tokens = _tokens(txn.email, txn.phone, txn.loyalty_id, txn.crm_id)
        if not tokens:
            continue  # anonymous walk-in: counted in coverage stats, not loaded
        identity = _ensure(tokens[0])
        identity.emails.update(filter(None, [txn.email]))
        identity.phones.update(filter(None, [txn.phone]))
        identity.loyalty_ids.update(filter(None, [txn.loyalty_id]))
        identity.crm_ids.update(filter(None, [txn.crm_id]))
        txn_to_key[txn.txn_id] = identity.key

    # Guardrail: quarantine implausible merges instead of writing them to Klaviyo.
    # A bad merge in a marketing platform is not a cosmetic bug — it sends a
    # Berlin VIP invitation to someone in Toronto, and it is hard to unpick.
    quarantined = [i for i in identities.values()
                   if i.identifier_count > MAX_IDENTIFIERS_PER_CLUSTER]
    for identity in quarantined:
        identities.pop(identity.key, None)
    quarantined_keys = {i.key for i in quarantined}
    txn_to_key = {t: k for t, k in txn_to_key.items() if k not in quarantined_keys}

    return identities, txn_to_key, quarantined


def coverage_report(identities: dict[str, Identity], quarantined: list[Identity]) -> dict:
    by_signal = defaultdict(int)
    for identity in identities.values():
        by_signal[identity.identifier_count] += 1
    addressable = sum(1 for i in identities.values() if i.is_addressable)
    with_external = sum(1 for i in identities.values() if i.external_id)
    return {
        "identities_resolved": len(identities),
        "addressable_identities": addressable,
        "identities_with_external_id": with_external,
        "external_id_coverage_pct": round(100 * with_external / max(len(identities), 1), 1),
        "multi_identifier_clusters": sum(v for k, v in by_signal.items() if k > 1),
        "quarantined_clusters": len(quarantined),
    }
