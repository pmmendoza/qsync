#!/usr/bin/env python3
"""
Split newsmem_recog question pairs into individual blocks
and wrap them in a BlockRandomizer in the survey flow.

Usage:
    python scripts/split_recog_blocks.py --survey-id SV_0qhBzbALspetCnk --account damian
    python scripts/split_recog_blocks.py --survey-id SV_0qhBzbALspetCnk --account damian --dry-run
"""

import argparse
import json
import os
import sys
import copy
from pathlib import Path

# Add qsync to path
QSYNC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(QSYNC_ROOT / "src"))

from qsync.api_push import send_api_request


# ── Configuration ──────────────────────────────────────────────────────

NEWSMEMORY_BLOCK_ID = "BL_bEBNoi3ynL4qR1A"

# Profile definitions: pre-survey vs post-survey have different QID ranges
PROFILES = {
    "pre": {
        "recog_pairs": [
            ("QID83",  "QID84"),   # pair 1
            ("QID85",  "QID86"),   # pair 2
            ("QID87",  "QID88"),   # pair 3
            ("QID89",  "QID90"),   # pair 4
            ("QID91",  "QID92"),   # pair 5
            ("QID93",  "QID94"),   # pair 6
            ("QID95",  "QID96"),   # pair 7
            ("QID97",  "QID98"),   # pair 8
            ("QID99",  "QID100"),  # pair 9
            ("QID101", "QID102"),  # pair 10
            ("QID103", "QID104"),  # pair 11
            ("QID105", "QID106"),  # pair 12
            ("QID107", "QID108"),  # pair 13
            ("QID109", "QID110"),  # pair 14
        ],
        # QIDs that come AFTER the recog pairs in the original block
        "post_recog_qids": ["QID39", "QID78", "QID7", "QID37"],
        # First recog QID (used to detect where pairs start)
        "first_recog_qid": "QID83",
    },
    "post": {
        "recog_pairs": [
            ("QID90",  "QID91"),   # pair 1
            ("QID92",  "QID93"),   # pair 2
            ("QID94",  "QID95"),   # pair 3
            ("QID96",  "QID97"),   # pair 4
            ("QID98",  "QID99"),   # pair 5
            ("QID100", "QID101"),  # pair 6
            ("QID102", "QID103"),  # pair 7
            ("QID104", "QID105"),  # pair 8
            ("QID106", "QID107"),  # pair 9
            ("QID108", "QID109"),  # pair 10
            ("QID110", "QID111"),  # pair 11
            ("QID112", "QID113"),  # pair 12
            ("QID114", "QID115"),  # pair 13
            ("QID116", "QID117"),  # pair 14
        ],
        # QIDs after recog pairs: timer + salience + timing
        "post_recog_qids": ["QID39", "QID7", "QID37"],
        # QID89 is intro text before pairs; QID90 is first actual pair QID
        "first_recog_qid": "QID90",
        # QID89 (newsmem_rec_text) is a static intro that stays in pre-recog
        "pre_recog_includes": ["QID89"],
    },
}


def get_profile_config(profile_name: str) -> dict:
    """Get the configuration for a profile."""
    profile = PROFILES[profile_name]
    recog_pairs = profile["recog_pairs"]
    post_recog_qids = profile["post_recog_qids"]
    all_recog_qids = set()
    for rq, cq in recog_pairs:
        all_recog_qids.add(rq)
        all_recog_qids.add(cq)
    return {
        "recog_pairs": recog_pairs,
        "post_recog_qids": post_recog_qids,
        "all_recog_qids": all_recog_qids,
        "first_recog_qid": profile["first_recog_qid"],
        "pre_recog_includes": set(profile.get("pre_recog_includes", [])),
    }


def get_api_config(account: str) -> tuple[str, dict]:
    """Load API config using qsync's own config resolution."""
    from qsync.config import load_account_env, build_headers

    env = load_account_env(account, root=QSYNC_ROOT)
    base_url = env.get("QUALTRICS_BASE_URL", "")
    headers = build_headers(env)
    return base_url, headers


def fetch_definition(survey_id: str, base_url: str, headers: dict) -> dict:
    """Fetch the live survey definition from the API."""
    resp = send_api_request(
        action="split_recog.fetch_definition",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        survey_id=survey_id,
        timeout=60,
    )
    return resp.json()["result"]


def create_block(
    survey_id: str, base_url: str, headers: dict,
    description: str, elements: list, dry_run: bool = False,
) -> str | None:
    """Create a new block via the API (two-step: POST + PUT). Returns the new block ID."""
    if dry_run:
        print(f"  [DRY-RUN] Would create block '{description}' with {len(elements)} elements")
        return None

    # Step 1: Create empty block
    create_payload = {
        "Type": "Standard",
        "Description": description,
    }
    resp = send_api_request(
        action="split_recog.create_block",
        method="POST",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/blocks",
        survey_id=survey_id,
        json=create_payload,
        timeout=60,
    )
    result = resp.json().get("result", {})
    block_id = result.get("BlockID", "")
    flow_id = result.get("FlowID", "")
    print(f"  Created block '{description}': {block_id} (flow: {flow_id})")

    # Step 2: Update with elements
    update_payload = {
        "Type": "Standard",
        "Description": description,
        "BlockElements": elements,
    }
    send_api_request(
        action="split_recog.update_new_block",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/blocks/{block_id}",
        survey_id=survey_id,
        json=update_payload,
        timeout=60,
    )
    print(f"    Updated {block_id} with {len(elements)} elements")
    return block_id


def update_block(
    survey_id: str, block_id: str, base_url: str, headers: dict,
    block_payload: dict, dry_run: bool = False,
) -> None:
    """Update an existing block via the API."""
    if dry_run:
        desc = block_payload.get("Description", block_id)
        n = len(block_payload.get("BlockElements", []))
        print(f"  [DRY-RUN] Would update block {block_id} ('{desc}') to {n} elements")
        return

    send_api_request(
        action="split_recog.update_block",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/blocks/{block_id}",
        survey_id=survey_id,
        json=block_payload,
        timeout=60,
    )
    print(f"  Updated block {block_id}")


def update_flow(
    survey_id: str, base_url: str, headers: dict,
    flow_payload: dict, dry_run: bool = False,
) -> None:
    """Update the survey flow via the API."""
    if dry_run:
        print(f"  [DRY-RUN] Would update survey flow")
        return

    send_api_request(
        action="split_recog.update_flow",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/flow",
        survey_id=survey_id,
        json=flow_payload,
        timeout=60,
    )
    print(f"  Updated survey flow")


def main():
    parser = argparse.ArgumentParser(description="Split recog pairs into randomized blocks")
    parser.add_argument("--survey-id", required=True, help="Target survey ID")
    parser.add_argument("--account", default="damian", help="qsync account name")
    parser.add_argument("--profile", choices=["pre", "post"], default="pre",
                        help="Survey profile: 'pre' (QID83-110) or 'post' (QID90-117)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without making changes")
    args = parser.parse_args()

    survey_id = args.survey_id
    dry_run = args.dry_run
    cfg = get_profile_config(args.profile)

    print(f"=== Split recog blocks for {survey_id} ===")
    if dry_run:
        print("  (DRY-RUN mode — no API writes)")
    print()

    # ── Step 0: Get API config ──
    base_url, headers = get_api_config(args.account)
    print(f"API: {base_url}")

    # ── Step 1: Fetch current definition ──
    print("\n[1/5] Fetching survey definition...")
    definition = fetch_definition(survey_id, base_url, headers)
    blocks = definition["Blocks"]
    flow = definition["SurveyFlow"]

    # Verify the newsmemory block exists and has expected content
    nm_block = blocks.get(NEWSMEMORY_BLOCK_ID)
    if not nm_block:
        raise SystemExit(f"ERROR: Block {NEWSMEMORY_BLOCK_ID} not found in survey")

    nm_elements = nm_block["BlockElements"]
    nm_qids = [e["QuestionID"] for e in nm_elements if e.get("Type") == "Question"]
    print(f"  Found newsmemory block with {len(nm_qids)} questions")

    # Verify all expected QIDs are present
    expected_qids = cfg["all_recog_qids"] | set(cfg["post_recog_qids"])
    missing = expected_qids - set(nm_qids)
    if missing:
        raise SystemExit(f"ERROR: Missing QIDs in newsmemory block: {missing}")
    n_recog = len(cfg["all_recog_qids"])
    n_post = len(cfg["post_recog_qids"])
    print(f"  All {n_recog} recog QIDs + {n_post} post-recog QIDs confirmed")

    # ── Step 2: Trim the original newsmemory block ──
    # Keep only pre-recog elements (everything before first recog pair QID)
    print("\n[2/5] Trimming newsmemory block (keep pre-recog questions only)...")
    trimmed_elements = []
    for el in nm_elements:
        if el.get("Type") == "Question":
            qid = el["QuestionID"]
            if qid in cfg["all_recog_qids"] or qid in cfg["post_recog_qids"]:
                break  # Stop at first recog question
        trimmed_elements.append(el)

    pre_recog_qids = [e["QuestionID"] for e in trimmed_elements if e.get("Type") == "Question"]
    print(f"  Pre-recog questions ({len(pre_recog_qids)}): {pre_recog_qids}")

    trimmed_block = copy.deepcopy(nm_block)
    trimmed_block["BlockElements"] = trimmed_elements
    update_block(survey_id, NEWSMEMORY_BLOCK_ID, base_url, headers, trimmed_block, dry_run)

    # ── Step 3: Create pair blocks ──
    n_pairs = len(cfg["recog_pairs"])
    print(f"\n[3/5] Creating {n_pairs} recog pair blocks...")
    pair_block_ids = []
    for i, (recog_qid, conf_qid) in enumerate(cfg["recog_pairs"], 1):
        desc = f"newsmem_recog_pair_{i:02d}"
        elements = [
            {"Type": "Question", "QuestionID": recog_qid},
            {"Type": "Question", "QuestionID": conf_qid},
        ]
        block_id = create_block(survey_id, base_url, headers, desc, elements, dry_run)
        pair_block_ids.append((block_id, desc))

    # ── Step 4: Create post-recog block ──
    print(f"\n[4/5] Creating post-recog block...")
    # Collect post-recog elements from the original block (with page breaks)
    post_elements = []
    in_post = False
    for el in nm_elements:
        if el.get("Type") == "Question" and el["QuestionID"] == cfg["post_recog_qids"][0]:
            in_post = True
        if in_post:
            post_elements.append(el)

    post_qids = [e["QuestionID"] for e in post_elements if e.get("Type") == "Question"]
    print(f"  Post-recog questions ({len(post_qids)}): {post_qids}")

    post_block_id = create_block(
        survey_id, base_url, headers,
        "newsmemory_post", post_elements, dry_run
    )

    # ── Step 5: Update the survey flow ──
    print(f"\n[5/5] Updating survey flow with BlockRandomizer...")

    # Find the current flow count to generate new FlowIDs
    flow_count = flow.get("Properties", {}).get("Count", 125)
    next_fl = flow_count + 1

    def next_flow_id():
        nonlocal next_fl
        fid = f"FL_{next_fl}"
        next_fl += 1
        return fid

    # Build the BlockRandomizer node
    randomizer_children = []
    for block_id, desc in pair_block_ids:
        if dry_run:
            # Use placeholder IDs for dry-run
            randomizer_children.append({
                "Type": "Standard",
                "ID": f"PLACEHOLDER_{desc}",
                "FlowID": next_flow_id(),
                "Autofill": [],
            })
        else:
            randomizer_children.append({
                "Type": "Standard",
                "ID": block_id,
                "FlowID": next_flow_id(),
                "Autofill": [],
            })

    randomizer_node = {
        "Type": "BlockRandomizer",
        "FlowID": next_flow_id(),
        "SubSet": n_pairs,  # Show all pairs
        "EvenPresentation": True,
        "Flow": randomizer_children,
    }

    # Post-recog block flow node
    post_recog_flow_node = {
        "Type": "Standard",
        "ID": post_block_id if not dry_run else "PLACEHOLDER_post",
        "FlowID": next_flow_id(),
        "Autofill": [],
    }

    # Now rebuild the flow: replace the single newsmemory Standard node
    # with: [trimmed newsmemory] + [BlockRandomizer] + [post-recog]
    new_flow_elements = []
    replaced = False
    for el in flow["Flow"]:
        if (
            el.get("Type") == "Standard"
            and el.get("ID") == NEWSMEMORY_BLOCK_ID
            and not replaced
        ):
            # Keep the trimmed newsmemory block reference
            new_flow_elements.append(el)
            # Insert BlockRandomizer after it
            new_flow_elements.append(randomizer_node)
            # Insert post-recog block after randomizer
            new_flow_elements.append(post_recog_flow_node)
            replaced = True
        else:
            new_flow_elements.append(el)

    if not replaced:
        raise SystemExit(f"ERROR: Could not find newsmemory block in flow")

    new_flow = copy.deepcopy(flow)
    new_flow["Flow"] = new_flow_elements
    new_flow["Properties"]["Count"] = next_fl - 1

    update_flow(survey_id, base_url, headers, new_flow, dry_run)

    # ── Summary ──
    print(f"\n=== Done! ===")
    print(f"  Original block trimmed to {len(pre_recog_qids)} pre-recog questions")
    print(f"  Created {n_pairs} pair blocks (each with recog + confidence)")
    print(f"  Created 1 post-recog block with {len(post_qids)} questions")
    print(f"  Flow updated: newsmemory → BlockRandomizer({n_pairs} pairs) → newsmemory_post")
    if dry_run:
        print(f"\n  Re-run without --dry-run to apply changes.")
    else:
        print(f"\n  Pull the survey again to verify:")
        print(f"    qsync --account {args.account} survey pull --survey-id {survey_id}")
        print(f"    qsync --account {args.account} flow pull --survey-id {survey_id}")


if __name__ == "__main__":
    main()
