# SD-WAN as a Service — Design

**Date:** 2026-05-17
**Repo:** `infrahub-demo-sp`
**Builds on:** `2026-05-15-sp-demo-mpls-l3vpn-design.md` (the L3VPN design this mirrors)

## 1. Goal

Add a second customer-facing service to the demo: **SD-WAN**, offered alongside the existing MPLS L3VPN. Both datasets (`financial`, `isp`) ship with one pre-loaded SD-WAN service. The Streamlit Service Catalog grows a **Create SD-WAN** form parallel to **Create L3VPN**. Two vendor canons render the per-edge config: **Cisco Viptela (cEdge / IOS-XE SD-WAN)** as the default, **Versa VOS** as an alternate.

## 2. Scope

### In scope

- New schema: `ServiceSdwan` + `ServiceSdwanSite`, plus a couple of new manufacturers / platforms / device-types to back the edge devices.
- Generator that materialises per-site SD-WAN edges: creates a new `DcimDevice` per `ServiceSdwanSite`, allocates a LAN-side IP inside the customer-supplied LAN subnet, attaches the device to the right vendor-specific edge group.
- One pre-loaded `ServiceSdwan` per dataset (different tenant from the L3VPN bootstrap), demonstrating both topologies: hub-spoke for `financial`, full-mesh for `isp`.
- Streamlit catalog: **Create SD-WAN** page; **Dashboard** lists L3VPNs and SD-WAN services in parallel tables.
- Two per-vendor transforms + templates rendering authentic-looking config for each edge device.
- Two artifact definitions (`sdwan-viptela-config`, `sdwan-versa-config`), each targeting its own per-vendor edge group.
- Two service checks (`sdwan_id_overlap`, `sdwan_site_subnet`) modelled on the existing L3VPN checks.
- Unit + catalog tests for the new transforms and form validators.
- Docs: new `docs/docs/services/sdwan.mdx`; schema-reference + quickstart updates; sidebar nav update.

### Out of scope

- SD-WAN control plane (vManage / vSmart / vBond / Versa Director / Analytics) — services render against an implicit control plane, but the controllers are not modelled as objects.
- Transport circuits (MPLS underlay vs Internet vs LTE) — edges have a LAN-side address only.
- Overlay tunnels and BGP — the per-vendor templates emit the *intent* (system identity, vpn 1 LAN config, SD-WAN block) but don't enumerate every fabric peer.
- Containerlab support for SD-WAN edges — the existing `clab-mpls-topology` artifact stays MPLS-only. The known-gap note in the new `services/sdwan.mdx` calls this out and the data wiring (a `data.ServiceSdwanSite.edges` loop on the clab template) is sketched but not implemented.

## 3. Architecture

### 3.1 Domain model

`ServiceSdwan` is a parallel of `ServiceL3Vpn`. It owns its `ServiceSdwanSite` children via a parent relationship, joins the `sdwans` `CoreStandardGroup` to trigger the generator, and exposes a single `vendor` knob that selects which fleet of edges + which artifact transform applies.

```text
Tenant ─owns─> ServiceSdwan ─parent─> ServiceSdwanSite ─sdwan_edge─> DcimDevice
                                                       └lan_subnet─> IpamPrefix (user-supplied)
                                                       └lan_address─> IpamIPAddress (generator-allocated)
                                                       └location─> LocationSite
```

The DcimDevice attached to each site has `role = cpe`, `status = active`, and a vendor-specific `platform` + `device_type`. The generator also adds the device to one of two vendor-scoped groups (`sdwan_viptela_edges`, `sdwan_versa_edges`); those groups are the targets the artifact definitions render against.

### 3.2 Component layout (parallel to L3VPN)

| Component | Path | Purpose |
|---|---|---|
| Schema | `schemas/sp/service_sdwan.yml` | `ServiceSdwan` + `ServiceSdwanSite` node definitions |
| Generator | `generators/generate_sdwan.py` | Materialise edges, LAN addresses, group memberships |
| Query (service) | `queries/service/sdwan.gql` | Used by the generator |
| Query (config) | `queries/config/sdwan_edge.gql` | Used by both transforms |
| Query (validation) | `queries/validation/sdwan_id_overlap.gql`, `sdwan_site_subnet.gql` | Used by the two checks |
| Transform (Viptela) | `transforms/sdwan_viptela.py` + `templates/sdwan_viptela.j2` | Cisco IOS-XE SD-WAN canon |
| Transform (Versa) | `transforms/sdwan_versa.py` + `templates/sdwan_versa.j2` | Versa VOS canon |
| Check | `checks/sdwan_id_overlap.py`, `checks/sdwan_site_subnet.py` | Service-level safety nets |
| Artifact defs | `.infrahub.yml` | `sdwan-viptela-config`, `sdwan-versa-config` |
| Bootstrap (shared) | `objects/00_manufacturers.yml`, `30_platforms.yml`, `40_device_types.yml`, `50_pools.yml`, `55_groups.yml` | Add Versa Networks, the two SD-WAN platforms, two device types, the number pool, the three groups |
| Bootstrap (dataset) | `objects/datasets/<name>/90_sdwan.yml` | One default `ServiceSdwan` per dataset |
| Catalog | `service_catalog/pages/2_Create_SDWAN.py` | Create-SD-WAN form |
| Validators | `service_catalog/utils/validators.py` | `validate_create_sdwan_form(...)` |
| Dashboard | `service_catalog/pages/0_Dashboard.py` | Add a second table for SD-WAN services |
| Menu | `menus/menu.yml` | Service Catalog → SD-WAN sidebar entry |
| Docs | `docs/docs/services/sdwan.mdx`, updates to `schema-reference.mdx`, `quickstart.mdx`, `sidebars.ts` | End-user documentation |

### 3.3 Schema details

#### `Service:Sdwan`

| Field | Kind | Required | Notes |
|---|---|---|---|
| `name` | Text | yes (unique) | Service name (e.g. `treasury-branch-sdwan`) |
| `description` | Text | no |  |
| `service_id` | Number | yes (unique) | Allocated from `sdwan_id_pool` |
| `vendor` | Dropdown | yes | `viptela` (default), `versa` |
| `topology` | Dropdown | yes | `hub-spoke` or `full-mesh` (default) |
| `status` | Dropdown | yes | `draft` → `active` → `decommissioned`, default `draft` |
| `tenant` (rel) | `OrganizationGeneric`, one, required | yes | Owning customer |
| `sites` (rel) | `ServiceSdwanSite`, many, component | no | Child sites |

HFID: `[name__value]`. Joins the `sdwans` CoreStandardGroup at create time (whether via bootstrap YAML or the catalog page) — this is what triggers `generate_sdwan`.

#### `Service:SdwanSite`

| Field | Kind | Required | Notes |
|---|---|---|---|
| `name` | Text | yes | Unique within parent service |
| `role` | Dropdown | yes | `hub`, `spoke` (default), `branch` |
| `status` | Dropdown | yes | `provisioning` → `active` → `decommissioned` |
| `lan_subnet` (rel) | `IpamPrefix`, one, required | yes | Customer LAN behind this edge |
| `lan_address` (rel) | `IpamIPAddress`, one, optional | no | Generator allocates first usable IP inside `lan_subnet` |
| `sdwan_edge` (rel) | `DcimDevice`, one, optional | no | Generator creates the edge device |
| `location` (rel) | `LocationSite`, one, required | yes | Which site/PoP the edge sits in |
| `sdwan` (parent) | `ServiceSdwan`, one, required | yes | Parent service. Identifier `sdwan__site`; mirrors L3VPN's `l3vpn` parent relationship. |

Uniqueness constraint: `[sdwan, name__value]` (mirrors L3VPN).

The `sites` relationship on `ServiceSdwan` uses the same `sdwan__site` identifier with `kind: Component`. None of the other relationships on `ServiceSdwanSite` need explicit `identifier:` values since each peer kind is referenced exactly once.

### 3.4 Generator

`generators/generate_sdwan.py` targets the `sdwans` group. For each `ServiceSdwan` member:

1. Allocate `service_id` from `sdwan_id_pool` if unset (matches the L3VPN pattern of pool-on-bootstrap-but-100-199-is-reserved).
2. For each `ServiceSdwanSite`:
   - If `sdwan_edge` is unset, create a `DcimDevice` named `<service.name>-<site.name>-edge`:
     - `platform` = `cisco_viptela` (vendor=viptela) or `versa_flexvnf` (vendor=versa)
     - `device_type` = `cEdge-1000` or `FlexVNF-200`
     - `role = cpe`, `status = active`, `location = site.location`
   - Add the device to `sdwan_viptela_edges` or `sdwan_versa_edges` (the artifact target).
   - If `lan_address` is unset, parse `site.lan_subnet.prefix`, take the first usable IP, create an `IpamIPAddress` (CIDR-formatted to match `lan_subnet`'s prefix length), bind to the device's LAN interface.
   - Bind `site.sdwan_edge`, `site.lan_address`; flip `site.status = active`.
3. Flip `service.status = active`.

Idempotent — re-running with the same data is a no-op. Pulls one shared helper from `generators/common.py` (`find_or_create_device(...)`) to keep the create-or-reuse logic out of the main flow.

### 3.5 Per-vendor transforms

Both transforms render config for **one edge device** (the artifact target). They share the same GraphQL query (`queries/config/sdwan_edge.gql`), which returns:

- The edge device (name, location, platform).
- Its `ServiceSdwanSite` (with `lan_subnet`, `lan_address`, `role`).
- The parent `ServiceSdwan` (`service_id`, `vendor`, `topology`).
- Sibling sites (for emitting comments listing peer locations — no actual tunnel config).

#### `sdwan_viptela` → IOS-XE SD-WAN

Output looks like (abbreviated):

```text
! Cisco IOS-XE SD-WAN config for {{ device.name }}
system
 host-name {{ device.name }}
 system-ip 10.10.0.{{ service.service_id }}
 site-id {{ service.service_id }}{{ site_index }}
 organization-name "infrahub-demo-sp"
!
sdwan
 ! topology: {{ service.topology }}
!
vpn 0
 interface ge0/0
  ip address dhcp-client
  tunnel-interface
   encapsulation ipsec
!
vpn 1
 interface ge0/1
  ip address {{ site.lan_address }}
```

#### `sdwan_versa` → Versa VOS

Output looks like:

```text
# Versa VOS config for {{ device.name }}
set orgs org-services {{ tenant.name }} virtual-router default
set orgs org-services {{ tenant.name }} virtual-router default interfaces tvi-0/0
set orgs org-services {{ tenant.name }} virtual-router LAN interfaces vni-0/0
set orgs org-services {{ tenant.name }} virtual-router LAN routing-options ...
```

Templates inherit `_macros.j2` for the `ip_only()` helper (already used by the PE templates).

### 3.6 Checks

Both target the `sdwans` group.

- **`sdwan_id_overlap`** (Python): scan every `ServiceSdwan`, flag duplicate `service_id` values. Safety net behind the pool — same shape as `l3vpn_overlap`.
- **`sdwan_site_subnet`** (Python): per service, ensure no two `ServiceSdwanSite` LAN prefixes overlap. Pre-existing helper from `checks/l3vpn_site_subnet.py` is the model.

Both use the truthy-dict null-safety pattern (`(node.get("foo") or {}).get("node")`) established in earlier fixes — sites whose generator hasn't run yet won't crash the check.

## 4. Bootstrap data

### 4.1 New shared bootstrap rows

- `objects/00_manufacturers.yml`: add `Versa Networks` (Cisco is already there).
- `objects/30_platforms.yml`: add `cisco_viptela` (manufacturer Cisco; `containerlab_os` unset; `netmiko_device_type: cisco_xe`) and `versa_flexvnf` (manufacturer Versa Networks; `containerlab_os` unset).
- `objects/40_device_types.yml`: add `cEdge-1000` (Cisco) and `FlexVNF-200` (Versa Networks).
- `objects/50_pools.yml`: add `sdwan_id_pool` (`CoreNumberPool`, `node = ServiceSdwan`, `node_attribute = service_id`, range `200-9999`; `100-199` reserved for bootstrap).
- `objects/55_groups.yml`: add `sdwans`, `sdwan_viptela_edges`, `sdwan_versa_edges`.

### 4.2 Per-dataset default service

`objects/datasets/financial/90_sdwan.yml`:
- Three `IpamPrefix` rows: `10.250.10.0/24`, `10.250.20.0/24`, `10.250.30.0/24` (the `10.250.0.0/16` block is reserved for SD-WAN LANs; disjoint from L3VPN's `10.200.0.0/16`).
- One `ServiceSdwan`:
  - `name: treasury-branch-sdwan`, `service_id: 100`, `vendor: viptela`, `topology: hub-spoke`
  - `tenant: treasury-ops` (different tenant from the L3VPN's `markets-trading`)
  - `member_of_groups: [sdwans]`
  - Three sites: `hub-london` (role `hub`, lan_subnet `10.250.10.0/24`, location `lon`), `spoke-frankfurt`, `spoke-amsterdam`.

`objects/datasets/isp/90_sdwan.yml`:
- Same three `IpamPrefix` rows.
- One `ServiceSdwan`:
  - `name: flo-streaming-overlay`, `service_id: 100`, `vendor: viptela`, `topology: full-mesh`
  - `tenant: flo-streaming`
  - `member_of_groups: [sdwans]`
  - Three sites: `pop-london`, `pop-frankfurt`, `pop-paris` (all role `spoke`, locations `lon` / `fra` / `par`).

## 5. Service Catalog (Streamlit)

### 5.1 New page

`service_catalog/pages/2_Create_SDWAN.py`. Mirrors `1_Create_L3VPN.py` exactly:

**Form**
- Service basics: `name`, `description`, `tenant` (dropdown), `vendor` (radio Viptela/Versa, default Viptela), `topology` (radio hub-spoke/full-mesh, default full-mesh)
- Sites: 2–6 rows; each row has `name`, `role`, `location` (dropdown of `LocationSite`), `lan_subnet` (CIDR text input)
- Submit → "Create SD-WAN service"

**Validator** (`service_catalog/utils/validators.py: validate_create_sdwan_form`)
- ≥2 sites; if `topology == hub-spoke`, exactly one site marked `hub`
- Site names unique within the service
- LAN subnets parse as valid CIDRs and are pairwise disjoint
- Each site picks a unique `location`

**On submit**
1. Validate; show errors and stop on failure.
2. `branch = client_main.branch.create(f"sdwan/{uuid.uuid4().hex[:8]}", sync_with_git=False)`.
3. Fetch the `sdwans` group on the new branch; fetch `sdwan_id_pool`.
4. Create the `ServiceSdwan` with `member_of_groups=[group.id]`, `vendor`, `topology`, `service_id=pool`.
5. For each site: create `IpamPrefix` for `lan_subnet`, then create the `ServiceSdwanSite` (parent → service).
6. Poll until `service.status == active` (120s timeout) — gives the generator time to materialise edges + LAN addresses.
7. POST `/api/artifact/generate/<def_id>?branch=<branch>` for every `CoreArtifactDefinition` — covers PE configs (unchanged), the two SD-WAN artifacts, and the clab topology.
8. Create `CoreProposedChange` against `main`; surface UI links.

### 5.2 Dashboard update

`service_catalog/pages/0_Dashboard.py` grows a second table below the existing L3VPN one, listing each `ServiceSdwan` with columns: `name`, `tenant`, `vendor`, `topology`, `service_id`, `# sites`. Same `l3vpn__ids` → `service_sdwan__ids` filter pattern as the L3VPN site-count column.

### 5.3 Sidebar nav

`menus/menu.yml` gains a Service Catalog → SD-WAN section parallel to → L3 VPNs.

## 6. Bootstrap + invoke wiring

### 6.1 `tasks.py`

The existing sorted-glob in `bootstrap` already loads `objects/datasets/<name>/90_sdwan.yml` automatically — no glob change.

One addition: after the existing `c.run("uv run python scripts/run_generator.py generate_l3vpn", ...)` step, add `c.run("uv run python scripts/run_generator.py generate_sdwan", ...)`. Order matters because L3VPN's generator can fail-fast on shared resources (less of a concern with disjoint pools, but predictable).

`scripts/regenerate_artifacts.py` already iterates every `CoreArtifactDefinition`; the two new SD-WAN definitions and the existing clab + PE definitions are all picked up unchanged.

### 6.2 No new invoke task

`invoke bootstrap` / `invoke init` cover the new path. No new flag. The dataset selection (`INFRAHUB_DATASET`) already controls which `90_sdwan.yml` overlay loads.

## 7. Tests

`tests/unit/test_transforms/test_sdwan_viptela.py` and `test_sdwan_versa.py` — fixture-driven snapshot tests (one `data` dict per test) covering:
- Renders `system / host-name <device>`
- Renders the correct `service_id` in `system-ip`
- Renders the `vpn 1 / interface ge0/1 / ip address <lan>` block (Viptela) or the equivalent Versa virtual-router block
- Topology-aware comments / hints (hub-spoke vs full-mesh)

`tests/catalog/test_validators.py` — extend with `test_sdwan_*` covering: ≥2 sites, hub-required when hub-spoke, unique site names, overlapping LAN subnets, garbage CIDR, unique location per site, happy path.

## 8. Documentation

- `docs/docs/services/sdwan.mdx` — new page paralleling `services/l3vpn.mdx`. Sections: overview, schema link, service lifecycle, vendor differences, per-vendor config example, checks, known gaps (clab support).
- `docs/docs/schema-reference.mdx` — add `ServiceSdwan` and `ServiceSdwanSite` sections (paralleling the L3VPN ones); add `sdwan_id_pool` row (range 200–9999) under Resource Pools.
- `docs/docs/quickstart.mdx` — one paragraph at the end of step 4 mentioning the SD-WAN catalog page alongside the L3VPN one; configuration table is unchanged (no new env vars introduced by this design).
- `docs/sidebars.ts` — link the new `services/sdwan.mdx` next to `services/l3vpn.mdx`.

## 9. Known gaps (intentionally deferred)

- No SD-WAN controllers modelled (vManage / vSmart / vBond / Versa Director).
- No transport circuits modelled (the demo doesn't distinguish MPLS underlay from Internet from LTE).
- No overlay tunnels / overlay BGP — the per-vendor templates emit the LAN-side config and the SD-WAN intent block, but the fabric peer list is comments only.
- Clab topology artifact stays MPLS-only — SD-WAN edges are not lab-deployable in this iteration.

## 10. Compatibility with the existing L3VPN flow

- No shared state between L3VPN and SD-WAN: separate pools, separate groups, separate generator, separate target group, separate artifact definitions.
- Tenant relationships are independent — a tenant can have one, both, or neither service.
- Customer LAN subnet space is partitioned: `10.200.0.0/16` for L3VPN, `10.250.0.0/16` for SD-WAN. `customer_subnet` ↔ `lan_subnet` are different relationships, no naming clash.
- The existing `l3vpn_site_subnet` and SD-WAN's `sdwan_site_subnet` checks operate independently; neither catches a *cross-service* overlap. That's intentional for v1 — overlapping a customer LAN between an L3VPN site and an SD-WAN site is a legitimate (if unusual) configuration.
