# Concurrency scenarios

Four short walkthroughs that exercise the intent-driven service catalog
under parallel proposed-change branches. Each YAML payload is a
ready-to-load intent — `infrahubctl object load …` it on the named
branch, then run the generator and observe the result.

The scenarios are independent: you can run any one of them against a
freshly bootstrapped repo (`uv run invoke init`). They assume the
default `financial` dataset is loaded — adjust tenants and band as
needed if you bootstrap with `isp` instead.

| Scenario | What it shows | Expected outcome |
| --- | --- | --- |
| **Q1 — disjoint intents** | Two intents on parallel branches, no shared resources. | Both merge cleanly. Distinct vpn_ids allocated from the same band-scoped pool. |
| **Q2 — shared PE oversubscription** | Two intents both binding sites to the same PE, exceeding its free-interface budget. | The `pe_interface_alloc` check fires on the second merge — first branch is fine, second is blocked. |
| **Q3 — pool race** | Many intents in parallel against the same band pool. | All allocate without collision; the pool's range guarantees enough IDs. |
| **Q4 — edit-after-active** | An intent that's already `active` on main gets edited on a branch. | `intent_immutability` check blocks the merge. |

## How to run a scenario

```bash
# Pick a scenario — e.g. Q1 branch A
infrahubctl branch create pc/q1-acme
infrahubctl object load examples/concurrency/q1_disjoint_branch_a.yml --branch pc/q1-acme

# Trigger the generator on the branch
infrahubctl generator generate_l3vpn --branch pc/q1-acme name=cust-q1-a

# Open a proposed change in the UI to review the realised graph
```

Run the matching `_branch_b.yml` on a second branch in parallel, then
merge both and watch the checks.
