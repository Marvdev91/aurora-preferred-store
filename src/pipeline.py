"""
CLI entry point.

    python -m src.pipeline generate            # build synthetic source extracts
    python -m src.pipeline build               # normalise, resolve, score (no network)
    python -m src.pipeline load --dry-run      # write the exact payloads to disk
    python -m src.pipeline load                # push profiles + events to Klaviyo
    python -m src.pipeline segments            # create the three segments
    python -m src.pipeline verify              # read back and prove it landed
    python -m src.pipeline all --dry-run       # end to end

`build` runs with no API key and no network, which is how the scoring logic gets
tested in CI. Everything that touches Klaviyo is behind `load` / `segments`.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import config, generate_source_data, identity, load as loader, normalize, preferred_store, segments
from .klaviyo import KlaviyoClient

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
log = logging.getLogger("pipeline")


def _banner(title: str):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def cmd_generate(args):
    counts = generate_source_data.generate()
    _banner("SOURCE EXTRACTS GENERATED")
    for name, count in counts.items():
        print(f"  {name:<28} {count:>6} records")


def cmd_build(args):
    transactions, crm, source_stats = normalize.load_all()
    identities, txn_to_key, quarantined = identity.resolve(transactions, crm)
    id_stats = identity.coverage_report(identities, quarantined)
    profiles, profile_stats = preferred_store.build_profiles(identities, transactions, txn_to_key)
    events = preferred_store.build_in_store_events(transactions, txn_to_key, identities)

    (DATA_DIR / "profiles.json").write_text(json.dumps(profiles, indent=2))
    (DATA_DIR / "events.json").write_text(json.dumps(events, indent=2))
    report = {"source": source_stats, "identity": id_stats, "profiles": profile_stats,
              "events_built": len(events)}
    (DATA_DIR / "build_report.json").write_text(json.dumps(report, indent=2))

    _banner("BUILD REPORT")
    for section, values in report.items():
        if isinstance(values, dict):
            print(f"\n  {section.upper()}")
            for key, value in values.items():
                print(f"    {key:<36} {value}")
        else:
            print(f"\n  {section.upper():<38} {values}")

    print("\n  Sample profile:")
    sample = next((p for p in profiles if p["properties"].get("preferred_store_id")), profiles[0])
    print("    " + json.dumps(sample, indent=2).replace("\n", "\n    "))


def _require_build():
    if not (DATA_DIR / "profiles.json").exists():
        sys.exit("No profiles.json — run `python -m src.pipeline build` first.")


def cmd_load(args):
    _require_build()
    profiles = json.loads((DATA_DIR / "profiles.json").read_text())
    events = json.loads((DATA_DIR / "events.json").read_text())

    client = KlaviyoClient(dry_run=args.dry_run)
    if not args.dry_run:
        account = client.get_account()
        account_id = (account.get("data") or [{}])[0].get("id", "unknown")
        log.info("Authenticated against Klaviyo account %s (revision %s)",
                 account_id, client.revision)

    profile_result = loader.load_profiles(client, profiles)
    if profile_result.get("dropped_profiles"):
        _banner("PROFILES DROPPED (no identifier survived repair)")
        for entry in profile_result["dropped_profiles"]:
            print(f"  #{entry['profile_index']:<4} {entry.get('email') or '(no email)':<45} {entry['reason']}")
    event_result = loader.load_events(client, events, limit=args.max_events)
    client.close()

    _banner("LOAD RESULT")
    print(json.dumps({"profiles": profile_result, "events": event_result}, indent=2))


def cmd_segments(args):
    client = KlaviyoClient(dry_run=args.dry_run)
    results = segments.sync(client)
    client.close()
    _banner("SEGMENTS")
    for result in results:
        print(f"  {result['name']}")
        print(f"      id={result.get('id', '-')}  {result.get('error', '')}")
        print(f"      why: {result['why']}")


def cmd_verify(args):
    """Read back from Klaviyo and prove the properties actually landed."""
    _require_build()
    profiles = json.loads((DATA_DIR / "profiles.json").read_text())
    targets = [p["email"] for p in profiles
               if p.get("email") and p["properties"].get("preferred_store_id")][:args.sample]

    client = KlaviyoClient()
    _banner("VERIFICATION — read-back from Klaviyo")
    for email in targets:
        response = client.get_profiles(filter_expr=f'equals(email,"{email}")')
        records = response.get("data", [])
        if not records:
            print(f"  MISSING  {email}")
            continue
        props = records[0]["attributes"].get("properties", {})
        print(f"  OK       {email}")
        print(f"             preferred_store_name  = {props.get('preferred_store_name')}")
        print(f"             confidence            = {props.get('preferred_store_confidence')}")
        print(f"             preferred_locale      = {props.get('preferred_locale')}")
        print(f"             orders_in_store_12m   = {props.get('orders_in_store_12m')}")
    client.close()


def cmd_all(args):
    cmd_generate(args)
    cmd_build(args)
    cmd_load(args)
    cmd_segments(args)


def main():
    # Shared flags live on a parent parser so they work *after* the subcommand,
    # i.e. `pipeline load --dry-run` rather than `pipeline --dry-run load`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Never call Klaviyo; write the payloads to data/dry_run_requests.jsonl")
    common.add_argument("--max-events", type=int, default=None,
                        help="Cap the number of events sent (useful for a first smoke test)")
    common.add_argument("--sample", type=int, default=5, help="Profiles to read back in verify")

    parser = argparse.ArgumentParser(description="Aurora Home Goods — Preferred Store POC",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in [("generate", cmd_generate), ("build", cmd_build),
                          ("load", cmd_load), ("segments", cmd_segments),
                          ("verify", cmd_verify), ("all", cmd_all)]:
        sub.add_parser(name, parents=[common]).set_defaults(func=handler)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
