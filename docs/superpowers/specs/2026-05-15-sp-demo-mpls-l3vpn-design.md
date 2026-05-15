# SP Demo: MPLS Backbone with L3VPN as a Service — Design

**Date:** 2026-05-15
**Repo:** `sp-demo-mpls-l3vpn`
**Reference demo:** [`infrahub-demo-dc`](https://docs.infrahub.app/demo-dc) (`/Users/pete/src/infrahub-demo-dc`)
**Schema foundation:** [`schema-library`](https://github.com/opsmill/schema-library) (`/Users/pete/src/schema-library`)

## 1. Goal

Build an Infrahub demo repository for service-provider networking, mirroring the shape and developer experience of `infrahub-demo-dc`. The headline use case is **defining a Layer-3 VPN as a service** on top of a multi-vendor MPLS backbone. A Streamlit "Service Catalog" sidecar provides the form-driven create flow, and a containerlab artifact lets users spin up part of the backbone locally to verify the rendered configs.

## 2. Scope

### In scope (v1)

- Schema for an MPLS backbone using classic **ISIS (L2) + LDP + MP-BGP VPNv4/VPNv6**.
- Four PEs, one per vendor — **Arista EOS, Cisco IOS-XR, Juniper Junos, Nokia SR OS** — in a full iBGP mesh (no P, no RR).
- Schema and generator for **`ServiceL3Vpn`** with multiple `ServiceL3VpnSite` rows; PE-CE routing via **eBGP, static, or connected**.
- Endpoint-only customer modelling (no CE device object in the schema).
- Streamlit Service Catalog with two pages: **Dashboard** + **Create L3VPN**.
- Containerlab topology artifact (Arista cEOS + Nokia SR Linux + Linux CEs) plus an `invoke lab.*` wrapper.
- Per-vendor config artifacts for all four PEs.
- Pytest unit + integration + catalog test suites.

### Out of scope (v1)

Documented in `docs/architecture.md` as future extensions:

- SR-MPLS / Segment Routing.
- Dedicated P routers and Route Reflectors.
- Multi-RT VPNs (hub-spoke, extranet) — v1 supports a single import RT and single export RT per VPN.
- OSPF as a PE-CE protocol.
- Mandatory CE device modelling.
- Add-Site / Decommission flows in the catalog.
- Lab deployment of Cisco IOS-XR / Juniper Junos PEs (no free images).
- Multi-region or inter-AS L3VPN (Option A/B/C).

## 3. Architecture overview

The project follows Infrahub's standard SDK pattern (the same one used in `infrahub-demo-dc`):

```
Schema definition  →  Bootstrap data  →  Generators  →  Transforms  →  Artifacts
                                            ↓
                                          Checks
```

A user's create-L3VPN journey:

1. Open Streamlit Service Catalog → Create L3VPN.
2. Submit form → catalog allocates `vpn_id` from a Number Pool, creates a branch, creates `ServiceL3Vpn` + `ServiceL3VpnSite` rows on the branch, and opens a `CoreProposedChange` against `main`.
3. The proposed-change pipeline runs: the **L3VPN generator** materialises VRFs / PE interfaces / IPs / BGP sessions; per-vendor **config transforms** render artifacts; **checks** validate.
4. User reviews the diff in Infrahub → merges → `main` reflects the new service.

The MPLS backbone itself is **static demo data**, pre-loaded by `invoke bootstrap`. This keeps the headline story focused on the L3VPN service.

## 4. Schemas

### 4.1 Directory layout

```
schemas/
  base/                                 # copied verbatim from /Users/pete/src/schema-library/base
    dcim.yml                            # GenericDevice, PhysicalDevice, Device, DeviceType, Platform,
                                        # Interface, InterfacePhysical, InterfaceVirtual,
                                        # InterfaceLayer2, InterfaceLayer3, ...
    organization.yml                    # OrganizationGeneric, OrganizationManufacturer, OrganizationProvider
    location.yml                        # LocationGeneric, LocationHosting
    ipam.yml                            # IpamPrefix, IpamIPAddress

  extensions/                           # copied from /Users/pete/src/schema-library/extensions
    vrf/vrf.yml                         # IpamVRF (vrf_rd, import_rt, export_rt), IpamRouteTarget
    routing/routing.yml                 # RoutingProtocol generic (device + vrf + status)
    routing_bgp/bgp.yml                 # RoutingAutonomousSystem, RoutingBGPPeerGroup, RoutingBGPSession
    topology/topology.yml               # TopologyGeneric

  sp/                                   # NEW — SP-demo-specific
    mpls.yml                            # MplsIsisProcess, MplsLdpProcess, MplsBgpProcess
    topology_mpls.yml                   # TopologyMplsBackbone
    service_l3vpn.yml                   # ServiceL3Vpn, ServiceL3VpnSite
    dcim_role_pe.yml                    # extends DcimDevice.role enum to add pe / p / rr
```

### 4.2 Reuse from `schema-library`

`base/dcim.yml` already provides:

- `DcimGenericDevice` with `platform → DcimPlatform`, `interfaces`, `primary_address`.
- `DcimPhysicalDevice` with `device_type → DcimDeviceType`, `location → LocationHosting`.
- `DcimDevice` inheriting both above plus `CoreArtifactTarget`, with a `status` and `role` dropdown.
- `DcimPlatform` with `containerlab_os`, `nornir_platform`, `napalm_driver`, `netmiko_device_type`, `ansible_network_os`.
- `DcimDeviceType` with a `manufacturer → OrganizationManufacturer` relationship.
- `InterfacePhysical` and `InterfaceVirtual` inheriting `InterfaceLayer2` + `InterfaceLayer3`.

`extensions/vrf/vrf.yml` provides `IpamVRF` (with `vrf_rd`, `import_rt`, `export_rt`) and `IpamRouteTarget`.

`extensions/routing_bgp/bgp.yml` provides `RoutingAutonomousSystem`, `RoutingBGPPeerGroup`, `RoutingBGPSession`. These cover both the iBGP backbone overlay (session_type INTERNAL) and PE-CE eBGP (session_type EXTERNAL).

`extensions/routing/routing.yml` provides the `RoutingProtocol` generic, which carries `device`, `vrf`, and `status`.

`extensions/topology/topology.yml` provides the `TopologyGeneric` generic.

The Device → Manufacturer relationship flows through `DcimDevice → DcimGenericDevice.platform → DcimPlatform.manufacturer` and through `DcimDevice → DcimPhysicalDevice.device_type → DcimDeviceType.manufacturer`. No new direct relationship is added.

### 4.3 New SP-specific schemas

**`sp/mpls.yml`** — IGP/LDP/MP-BGP process nodes inheriting `RoutingProtocol`:

- `MplsIsisProcess` — `area_id` (Text, default `49.0001`), `level` (Dropdown: `level-1`, `level-2`, `level-1-2`; default `level-2`), `net_id` (Text, derived from device loopback). Relationship `interfaces → InterfacePhysical` (many) for ISIS-enabled interfaces.
- `MplsLdpProcess` — `router_id` (Text, mirrors loopback), `transport_address` (relationship to `IpamIPAddress` = loopback). Relationship `interfaces → InterfacePhysical` (many) for LDP-enabled interfaces.
- `MplsBgpProcess` — `router_id`, `address_families` (Dropdown multi: `vpnv4`, `vpnv6`). Relationship `sessions → RoutingBGPSession` (many) for the iBGP overlay sessions.

**`sp/topology_mpls.yml`** — `TopologyMplsBackbone` inheriting `TopologyGeneric`:

- `name` (Text, unique)
- `asn → RoutingAutonomousSystem` (cardinality one)
- `isis_area` (Text, default `49.0001`)
- `isis_level` (Dropdown, default `level-2`)
- `pes → DcimDevice` (cardinality many)

**`sp/service_l3vpn.yml`**:

- `ServiceL3Vpn`:
  - `name` (Text, unique)
  - `description` (Text, optional)
  - `tenant → OrganizationGeneric` (cardinality one)
  - `vpn_id` (Number, unique) — allocated from `vpn_id_pool` (`CoreNumberPool`) at create time
  - `address_family` (Dropdown: `ipv4`, `ipv4_ipv6`; default `ipv4`)
  - `vrf → IpamVRF` (cardinality one) — created by the generator; holds the actual `vrf_rd`, `import_rt`, `export_rt`
  - `status` (Dropdown: `draft`, `active`, `decommissioned`; default `draft`)
  - `sites → ServiceL3VpnSite` (children, cardinality many)

- `ServiceL3VpnSite` (inherits `RoutingProtocol` for `status` + `device` + `vrf`):
  - `name` (Text)
  - `l3vpn` (parent, cardinality one)
  - `pe → DcimDevice` (cardinality one)
  - `pe_interface → InterfacePhysical` (cardinality one, allocated by generator)
  - `customer_subnet → IpamPrefix` (cardinality one)
  - `pe_address → IpamIPAddress` (cardinality one)
  - `ce_address → IpamIPAddress` (cardinality one)
  - `routing_protocol` (Dropdown: `ebgp`, `static`, `connected`)
  - `bgp_peer_asn` (Number, optional)
  - `static_routes` (TextArea / JSON, optional — list of `{prefix, next_hop}`)

**`sp/dcim_role_pe.yml`** — schema extension that adds `pe`, `p`, `rr` choices to `DcimDevice.role`.

### 4.4 Known v1 limitations

`IpamVRF.import_rt` and `export_rt` are `cardinality: one` in `schema-library`. v1 L3VPNs therefore use a single import RT and single export RT — sufficient for full-mesh VPNs. Hub-spoke and extranet are documented as future extensions.

## 5. Resource pools

Bootstrapped as instances (not schema nodes) by `objects/50_pools.yml`:

| Pool | Type | Range / Source | Drives |
|---|---|---|---|
| `vpn_id_pool` | `CoreNumberPool` | `100`–`9999` | `ServiceL3Vpn.vpn_id`; RD/RT derive as `<asn>:<vpn_id>` |
| `pe_loopback_pool` | `CoreIPAddressPool` | `10.0.0.0/24` | PE `Loopback0` addresses |
| `backbone_p2p_pool` | `CoreIPPrefixPool` | `10.1.0.0/16` → `/31` | PE-PE backbone links |
| `pe_ce_pool` | `CoreIPPrefixPool` | `10.100.0.0/16` → `/30` | PE-CE links per L3VPN site |

## 6. Bootstrap data

Loaded by `invoke bootstrap`, mirroring `infrahub-demo-dc`'s `objects/` layout:

```
objects/
  00_organizations.yml       Manufacturers (Arista, Cisco, Juniper, Nokia),
                             Provider (OpsMillNet), tenants (acme, contoso, globex)
  10_locations.yml           Region EMEA + 4 PoPs: LON, FRA, AMS, PAR
  20_asns.yml                AS 65000 for the backbone (RoutingAutonomousSystem)
  30_platforms.yml           4 DcimPlatform rows:
                               arista_eos     → containerlab_os: ceos
                               cisco_iosxr    → containerlab_os: (unset; no clab image)
                               juniper_junos  → containerlab_os: (unset; no clab image)
                               nokia_sros     → containerlab_os: srl   # SR Linux substitution
                             plus nornir_platform, napalm_driver, etc.
  40_device_types.yml        Arista 7280R3, Cisco NCS-540, Juniper MX204, Nokia 7250-IXR-R6
  50_pools.yml               CoreNumberPool + CoreIPAddressPool + CoreIPPrefixPool rows
  60_backbone.yml            4 PE Devices (pe-lon-arista, pe-fra-cisco, pe-ams-juniper,
                             pe-par-nokia) with Loopback0 + IPs from pe_loopback_pool;
                             6 backbone p2p links pre-cabled with /31s from backbone_p2p_pool;
                             ISIS/LDP/MP-BGP processes per PE;
                             6 iBGP RoutingBGPSession rows in full mesh
                             (generated by scripts/build_backbone_yaml.py from a small spec)
  70_topology.yml            One TopologyMplsBackbone row referencing the 4 PEs
  80_groups.yml              pe_arista_eos, pe_cisco_iosxr, pe_juniper_junos,
                             pe_nokia_sros, pes, l3vpns, topologies_mpls
```

## 7. Generators

Only one generator in v1: `L3VpnGenerator`.

**File:** `generators/generate_l3vpn.py`
**Targets:** `l3vpns` group
**Query:** `queries/service/l3vpn.gql`
**Class:** `L3VpnGenerator`
**Parameters:** `name: name__value`

For each `ServiceL3VpnSite`:

1. Allocate the next free `Physical` interface on `site.pe` (lowest-numbered with `role == free`). Set its role to `cust` and description to `L3VPN <vpn.name>`.
2. Allocate a `/30` from `pe_ce_pool`. Create two `IpamIPAddress` rows (`pe_address`, `ce_address`) bound to the new VRF.
3. Create (or reuse) the `IpamVRF` for the L3VPN: `name = vpn.name`, `vrf_rd = "<asn>:<vpn_id>"`. Create matching import/export `IpamRouteTarget` rows on first site for that VPN; reuse on subsequent sites.
4. If `routing_protocol == ebgp`: create a `RoutingBGPSession` row (session_type `EXTERNAL`, role `peering`, local_ip = `pe_address`, remote_ip = `ce_address`, local_as = backbone AS, remote_as = `bgp_peer_asn`, vrf = the new VRF).
5. Bind `customer_subnet` to the VRF.

The generator must be **idempotent** — re-running on the same input must not duplicate VRFs, interfaces, or sessions.

## 8. Transforms

### 8.1 Per-vendor PE config transforms

Python wrappers around Jinja2 templates (the demo-dc pattern). One per vendor:

```
transforms/
  pe_arista_eos.py           class PeAristaEos      → templates/pe_arista_eos.j2
  pe_cisco_iosxr.py          class PeCiscoIosXr     → templates/pe_cisco_iosxr.j2
  pe_juniper_junos.py        class PeJuniperJunos   → templates/pe_juniper_junos.j2
  pe_nokia_sros.py           class PeNokiaSrOs      → templates/pe_nokia_sros.j2
```

Each runs `template.render(data=query_result)` with `autoescape=False`. A shared `templates/_macros.j2` holds IP-prefix and RT formatters used by all four.

Each template renders, in order:

1. Header (hostname, banner).
2. `Loopback0` (router-id + iBGP source).
3. Backbone interfaces (IP + ISIS + LDP).
4. ISIS process (`router isis 1` / `protocols isis` / `configure router isis`).
5. LDP process (`mpls ldp` / `protocols ldp` / `configure router ldp`).
6. MP-BGP (router-bgp + VPNv4/VPNv6 families + iBGP neighbors).
7. Per L3VPN VRF on this device: VRF instance + RD/RTs.
8. Per L3VPN site on this device: PE-CE interface bound to VRF + IP + PE-CE routing.

Vendor-specific canon:

- **Arista EOS** — `vrf instance`, `router bgp 65000` with `vrf <name>` sub-block, `address-family vpn-ipv4`.
- **Cisco IOS-XR** — `vrf <name>`, `address-family vpnv4 unicast`, `route-policy` pass-all stubs.
- **Juniper Junos** — `routing-instances <name> { instance-type vrf; ... }`, `protocols bgp group vpnv4-rr`.
- **Nokia SR OS** — `configure service vprn <vpn_id> name <name>`, classic context-style config.

### 8.2 Containerlab transform

**File:** `transforms/clab_topology.py` (Python) + `templates/clab_topology.j2` (Jinja2)
**Targets:** `topologies_mpls` group
**Query:** `queries/topology/clab.gql`

Renders containerlab YAML containing:

- **Two PEs** — the Arista PE (clab kind `ceos`) and the Nokia PE (clab kind `srl`, substituted from `sros` because of licensing).
- **Backbone link** — one cabling row connecting Arista ↔ Nokia.
- **Linux CEs** — one `nicolaka/netshoot` container per `ServiceL3VpnSite` whose `pe.platform IN (arista_eos, nokia_sros)`. Each CE has `exec` lines setting its eth1 IP to the site's `ce_address` and a default route to the site's `pe_address`.

Substitution mapping is sourced from `DcimPlatform.containerlab_os`. A clear comment in the rendered YAML and in `docs/lab/containerlab.md` explains the `sros → srl` substitution: the SR OS *config artifact* still renders SR OS canon for the engineer audience, while the lab uses SR Linux for the runnable-without-license audience.

Cisco IOS-XR and Juniper Junos PEs are intentionally excluded from the lab.

## 9. Checks

```
checks/
  l3vpn_overlap.py             targets: l3vpns
    No two L3VPNs use the same RD; warn if import RTs collide without a documented hub-spoke intent.
  l3vpn_site_subnet.py         targets: l3vpns
    Within an L3VPN, no two site customer_subnets overlap.
  pe_interface_alloc.py        targets: pes
    Each L3VPN site claims exactly one Physical interface on its PE; no interface is double-claimed.
  backbone_session_count.py    targets: pes
    Each PE has exactly N-1 INTERNAL RoutingBGPSession rows (catches a torn-down mesh).
```

## 10. `.infrahub.yml` registry

```yaml
schemas:
  - schemas/base/
  - schemas/extensions/
  - schemas/sp/

menus:
  - menus/menu.yml

queries:
  - {name: pe_arista_eos,    file_path: queries/config/pe_arista_eos.gql}
  - {name: pe_cisco_iosxr,   file_path: queries/config/pe_cisco_iosxr.gql}
  - {name: pe_juniper_junos, file_path: queries/config/pe_juniper_junos.gql}
  - {name: pe_nokia_sros,    file_path: queries/config/pe_nokia_sros.gql}
  - {name: clab_topology,    file_path: queries/topology/clab.gql}
  - {name: l3vpn,            file_path: queries/service/l3vpn.gql}

python_transforms:
  - {name: pe_arista_eos,    class_name: PeAristaEos,    file_path: transforms/pe_arista_eos.py}
  - {name: pe_cisco_iosxr,   class_name: PeCiscoIosXr,   file_path: transforms/pe_cisco_iosxr.py}
  - {name: pe_juniper_junos, class_name: PeJuniperJunos, file_path: transforms/pe_juniper_junos.py}
  - {name: pe_nokia_sros,    class_name: PeNokiaSrOs,    file_path: transforms/pe_nokia_sros.py}
  - {name: clab_topology,    class_name: ClabTopology,   file_path: transforms/clab_topology.py}

artifact_definitions:
  - {name: pe-arista-eos-config,    artifact_name: pe-arista-eos,
     content_type: text/plain, targets: pe_arista_eos,
     transformation: pe_arista_eos,    parameters: {device: name__value}}
  - {name: pe-cisco-iosxr-config,   artifact_name: pe-cisco-iosxr,
     content_type: text/plain, targets: pe_cisco_iosxr,
     transformation: pe_cisco_iosxr,   parameters: {device: name__value}}
  - {name: pe-juniper-junos-config, artifact_name: pe-juniper-junos,
     content_type: text/plain, targets: pe_juniper_junos,
     transformation: pe_juniper_junos, parameters: {device: name__value}}
  - {name: pe-nokia-sros-config,    artifact_name: pe-nokia-sros,
     content_type: text/plain, targets: pe_nokia_sros,
     transformation: pe_nokia_sros,    parameters: {device: name__value}}
  - {name: clab-mpls-topology,      artifact_name: clab-mpls-topology,
     content_type: text/plain, targets: topologies_mpls,
     transformation: clab_topology,    parameters: {name: name__value}}

generator_definitions:
  - {name: generate_l3vpn, file_path: generators/generate_l3vpn.py,
     targets: l3vpns, query: l3vpn, class_name: L3VpnGenerator,
     parameters: {name: name__value}}

check_definitions:
  - {name: l3vpn_overlap,           class_name: L3VpnOverlapCheck,
     file_path: checks/l3vpn_overlap.py,           targets: l3vpns}
  - {name: l3vpn_site_subnet,       class_name: L3VpnSiteSubnetCheck,
     file_path: checks/l3vpn_site_subnet.py,       targets: l3vpns}
  - {name: pe_interface_alloc,      class_name: PeInterfaceAllocCheck,
     file_path: checks/pe_interface_alloc.py,      targets: pes}
  - {name: backbone_session_count,  class_name: BackboneSessionCountCheck,
     file_path: checks/backbone_session_count.py,  targets: pes}
```

## 11. Streamlit Service Catalog

### 11.1 Layout

```
service_catalog/
  Home.py                          st.navigation entry; sidebar logo + page registry
  Dockerfile                       python:3.12-slim + streamlit + infrahub-sdk
  requirements.txt                 streamlit, infrahub-sdk, pandas, python-dotenv, pyyaml, requests
  pages/
    0_Dashboard.py                 existing L3VPNs with branch selector
    1_Create_L3VPN.py              wizard form to define a new L3VPN
  utils/
    __init__.py                    client_for(branch), display_logo(), wait_for_generator()
  assets/
    logo.svg
```

### 11.2 Docker integration

`docker-compose.override.yml` adds a `streamlit-service-catalog` service under profile `service-catalog`, identical in shape to demo-dc:

- Build from `./service_catalog/Dockerfile`.
- Expose port `8501` (`STREAMLIT_PORT` overridable).
- Environment: `INFRAHUB_ADDRESS=http://infrahub-server:8000`, `INFRAHUB_UI_URL`, `INFRAHUB_API_TOKEN`, `DEFAULT_BRANCH=main`, `GENERATOR_WAIT_TIME=60`.

Started with `docker compose --profile service-catalog up`.

### 11.3 Page 1 — Dashboard (`0_Dashboard.py`)

- Branch selector at the top (default `main`, dropdown of `client.branch.all()`).
- Header tiles: count of active L3VPNs, count of sites, count of unique tenants.
- One `st.dataframe` listing L3VPNs: `name`, `tenant`, `vpn_id`, `RD`, `# sites`, `status`, `created`. Filter box on tenant.
- Expandable row per L3VPN → list of sites (`PE`, `site location`, `customer_subnet`, `routing_protocol`).
- Hyperlinks back to the Infrahub UI for each L3VPN and site (uses `INFRAHUB_UI_URL`).

### 11.4 Page 2 — Create L3VPN (`1_Create_L3VPN.py`)

Multi-step `st.form`:

**Step 1 — Service basics**

- `name` (text, required, validated unique against existing L3VPNs).
- `description` (text, optional).
- `tenant` (selectbox populated from `OrganizationGeneric` rows).
- `address_family` (radio: IPv4 / IPv4+IPv6).
- `vpn_id` is auto-allocated at submit; displayed read-only in Step 3.

**Step 2 — Sites** (dynamic list, "Add site" / "Remove" buttons; minimum 2 sites)

Per site:

- `name` (text).
- `pe` (selectbox: the 4 PEs, shown as `<name> (<platform>)`).
- `customer_subnet` (text, CIDR, validated).
- `routing_protocol` (radio: eBGP / Static / Connected).
- If eBGP: `bgp_peer_asn` (number, required).
- If Static: editable table of `{prefix, next_hop}` (at least one row).

**Step 3 — Review & Submit**

On submit:

1. `vpn_id = client.allocate_from_pool("vpn_id_pool")`.
2. `branch = client.branch.create(name=f"service/l3vpn-{vpn_id}", sync_with_git=False)`.
3. `client.create("ServiceL3Vpn", ...)` on the branch.
4. For each site: `client.create("ServiceL3VpnSite", l3vpn=..., ...)`.
5. `pc = client.create("CoreProposedChange", source_branch=branch.name, destination_branch="main", name=f"Create L3VPN {name}")`.
6. Show branch name, PC URL (link to Infrahub UI), spinner polling PC state until generator + checks complete or `GENERATOR_WAIT_TIME` elapses.
7. On success: green banner + "View in Infrahub" + "Back to Dashboard".

### 11.5 Client-side validation

Catches errors before opening a branch:

- L3VPN name unique.
- ≥ 2 sites.
- Customer subnets non-overlapping within this VPN.
- BGP peer ASN required when routing_protocol == eBGP.
- ≥ 1 static route when routing_protocol == Static.
- PE not reused across sites of *this* VPN (one site per PE per VPN).

Server-side checks (Section 9) cover cross-VPN concerns: RD/RT uniqueness, customer-subnet overlap across VPNs, PE interface allocation sanity, backbone session count.

## 12. Containerlab and `invoke lab.*`

### 12.1 Realities

- Lab includes only the **Arista (cEOS)** and **Nokia (SR Linux)** PEs + the one backbone link between them + Linux CEs for L3VPN sites on those two PEs.
- Cisco IOS-XR and Juniper Junos PEs are in the data model and get rendered config artifacts but are not deployed.
- The **SR OS → SR Linux substitution** is intentional. The pe-nokia-sros *config* artifact still renders SR OS canon. The lab uses SR Linux because no free license is required.
- Documented in `docs/lab/containerlab.md`.

### 12.2 `invoke lab.*` namespace

```
invoke lab.deploy         Download the latest clab-mpls-topology artifact via the SDK,
                          write to lab/mpls-backbone.clab.yml, run containerlab deploy.
invoke lab.destroy        containerlab destroy.
invoke lab.push-arista    Download pe-arista-eos artifact, push to running cEOS via netmiko.
                          (Nokia SR Linux runs with defaults; pushing SR-Linux YANG-style config
                          is a documented v1 gap.)
invoke lab.status         containerlab inspect.
```

`lab/` is gitignored except for `.gitkeep`; nothing rendered lands in git.

## 13. Tests

```
tests/
  unit/
    test_schemas.py                       schema parse, FK targets, RT regex
    test_generators/
      test_l3vpn.py                       mock client + sample input → assert created objects
    test_transforms/
      test_pe_arista_eos.py               render against fixture data → grep for key lines
      test_pe_cisco_iosxr.py
      test_pe_juniper_junos.py
      test_pe_nokia_sros.py
      test_clab_topology.py
    test_checks/                          one per check file
  integration/                            requires running Infrahub
    conftest.py                           fixtures: client, fresh branch per test
    test_bootstrap.py                     bootstrap completes; backbone reachable
    test_create_l3vpn.py                  SDK create L3VPN → wait for generator → assert artifacts rendered
  catalog/
    test_validators.py                    Streamlit form validators
```

Coverage target ≥ 70% (matches the user's global CLAUDE.md).

## 14. `tasks.py` (invoke)

```
invoke start                          docker compose up
invoke destroy                        docker compose down -v
invoke bootstrap                      infrahubctl schema load + object load + protocols
invoke init                           destroy → start → bootstrap → demo data
invoke lint                           ruff + mypy + yamllint + rumdl
invoke test [--kind=unit|integration|catalog]   pytest tests/<kind>
invoke lab.deploy | lab.destroy | lab.push-arista | lab.status
```

## 15. Final repo layout

```
sp-demo-mpls-l3vpn/
  .infrahub.yml  .env.example  .gitignore  .yamllint.yml  .vale.ini
  AGENTS.md  CLAUDE.md → AGENTS.md  README.md  LICENSE.txt
  docker-compose.override.yml  pyproject.toml  tasks.py  uv.lock

  schemas/{base/, extensions/, sp/}
  menus/menu.yml
  objects/00..80_*.yml
  queries/{config/, topology/, service/}
  generators/{generate_l3vpn.py, common.py, schema_protocols.py}
  transforms/{pe_arista_eos.py, pe_cisco_iosxr.py, pe_juniper_junos.py,
              pe_nokia_sros.py, clab_topology.py}
  templates/{pe_arista_eos.j2, pe_cisco_iosxr.j2, pe_juniper_junos.j2,
             pe_nokia_sros.j2, clab_topology.j2, _macros.j2}
  checks/{l3vpn_overlap.py, l3vpn_site_subnet.py, pe_interface_alloc.py,
          backbone_session_count.py}

  service_catalog/{Home.py, Dockerfile, requirements.txt,
                   pages/0_Dashboard.py, pages/1_Create_L3VPN.py,
                   utils/, assets/}

  lab/.gitkeep                          # runtime-only
  scripts/build_backbone_yaml.py        # regenerate 60_backbone.yml from a small spec

  tests/{unit/, integration/, catalog/}
  docs/{quickstart.md, architecture.md, schema-reference.md,
        services/l3vpn.md, lab/containerlab.md, AGENTS.md}
```

## 16. Implementation notes

- When writing the schema YAML files, invoke the `infrahub:infrahub-managing-schemas` skill — that's where the validate-against-Infrahub loop lives.
- Similarly: `infrahub-managing-objects` for bootstrap data, `infrahub-managing-generators` for the L3VPN generator, `infrahub-managing-transforms` for the per-vendor config transforms and clab transform, `infrahub-managing-checks` for the four checks, `infrahub-managing-menus` for the sidebar menu.
- After completing implementation, run `infrahub-auditing-repo` to verify the demo meets repo best-practice rules before declaring done.
- Python 3.12+, `uv` for dependency management, ruff + mypy + pytest, ≥ 70% coverage. All function signatures typed; Google-style docstrings on modules/classes/functions.

## 17. Out-of-scope (recap)

- SR-MPLS / Segment Routing.
- P routers and Route Reflectors.
- Multi-RT VPNs (hub-spoke, extranet).
- OSPF as a PE-CE protocol.
- Mandatory CE device modelling.
- Add-Site / Decommission flows in the catalog.
- Lab deployment of Cisco IOS-XR / Juniper Junos.
- Multi-region / inter-AS L3VPN.

These are listed in `docs/architecture.md` as the natural growth path for v2.
