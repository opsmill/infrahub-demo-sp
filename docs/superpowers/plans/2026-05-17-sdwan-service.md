# SD-WAN as a Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second customer-facing service (SD-WAN) alongside the existing L3VPN, with per-service vendor choice (Cisco Viptela default, Versa alternate), one default service per dataset, and a Streamlit catalog page mirroring Create L3VPN.

**Architecture:** New `ServiceSdwan` + `ServiceSdwanSite` schema with a parallel generator, two per-vendor transforms, two new artifact definitions, two checks, and a new catalog page. Uses the same patterns and helpers as L3VPN: `CoreStandardGroup` as generator target, pool-allocated `service_id`, idempotent generator, fixture-driven snapshot tests. Edge devices are created per-site by the generator (not pre-seeded).

**Tech Stack:** Infrahub schema YAML, Python (Infrahub SDK + generator/transform/check classes), Jinja2 templates, GraphQL queries, Streamlit catalog page, pytest.

**Reference design:** `docs/superpowers/specs/2026-05-17-sdwan-service-design.md`

---

## File Structure

**Schema (new)**
- `schemas/sp/service_sdwan.yml` — `ServiceSdwan` and `ServiceSdwanSite`

**Bootstrap (shared, modify)**
- `objects/00_manufacturers.yml` — add `Versa Networks`
- `objects/30_platforms.yml` — add `cisco_viptela`, `versa_flexvnf`
- `objects/40_device_types.yml` — add `cEdge-1000`, `FlexVNF-200`
- `objects/50_pools.yml` — add `sdwan_id_pool`
- `objects/55_groups.yml` — add `sdwans`, `sdwan_viptela_edges`, `sdwan_versa_edges`

**Bootstrap (per-dataset, new)**
- `objects/datasets/financial/90_sdwan.yml`
- `objects/datasets/isp/90_sdwan.yml`

**Generator (new + modify)**
- `generators/common.py` — add `find_or_create_device` helper
- `generators/generate_sdwan.py` — L3VPN-parallel generator

**Queries (new)**
- `queries/service/sdwan.gql` — generator data
- `queries/config/sdwan_edge.gql` — both transforms
- `queries/validation/sdwan_id_overlap.gql` — for `sdwan_id_overlap` check
- `queries/validation/sdwan_site_subnet.gql` — for `sdwan_site_subnet` check

**Transforms + templates (new)**
- `transforms/sdwan_viptela.py` + `templates/sdwan_viptela.j2`
- `transforms/sdwan_versa.py` + `templates/sdwan_versa.j2`

**Checks (new)**
- `checks/sdwan_id_overlap.py`
- `checks/sdwan_site_subnet.py`

**Registrations (modify)**
- `.infrahub.yml` — register the new queries, transforms, artifact definitions, generator, check definitions

**Bootstrap wiring (modify)**
- `tasks.py` — add `run_generator.py generate_sdwan` after the L3VPN generator step

**Catalog (new + modify)**
- `service_catalog/pages/2_Create_SDWAN.py` — new form
- `service_catalog/pages/0_Dashboard.py` — second table for SD-WAN
- `service_catalog/utils/validators.py` — `validate_create_sdwan_form`

**Menu (modify)**
- `menus/menu.yml` — Service Catalog → SD-WAN

**Tests (new + modify)**
- `tests/unit/test_transforms/fixtures.py` — add SD-WAN data fixtures
- `tests/unit/test_transforms/test_sdwan_viptela.py`
- `tests/unit/test_transforms/test_sdwan_versa.py`
- `tests/catalog/test_validators.py` — extend with `test_sdwan_*`

**Docs (new + modify)**
- `docs/docs/services/sdwan.mdx`
- `docs/docs/schema-reference.mdx` — add ServiceSdwan / ServiceSdwanSite + pool row
- `docs/docs/quickstart.mdx` — one paragraph in step 4
- `docs/sidebars.ts` — link the new service page

---

## Task 1: Schema — `ServiceSdwan` and `ServiceSdwanSite`

**Files:**
- Create: `schemas/sp/service_sdwan.yml`

- [ ] **Step 1: Create the schema file**

```yaml
---
# yaml-language-server: $schema=https://schema.infrahub.app/infrahub/schema/latest.json
version: "1.0"

nodes:
  - name: Sdwan
    namespace: Service
    description: An SD-WAN service offered to a tenant.
    label: SD-WAN
    icon: mdi:lan-connect
    include_in_menu: false
    human_friendly_id:
      - name__value
    display_label: name__value
    order_by:
      - name__value
    attributes:
      - name: name
        kind: Text
        unique: true
        order_weight: 1000
      - name: description
        kind: Text
        optional: true
        order_weight: 1100
      - name: service_id
        kind: Number
        unique: true
        order_weight: 1200
        description: "Allocated from sdwan_id_pool. Used for SD-WAN site-id derivation."
      - name: vendor
        kind: Dropdown
        default_value: "viptela"
        order_weight: 1300
        choices:
          - name: viptela
            label: Cisco Viptela
            color: "#1ba0d7"
          - name: versa
            label: Versa Networks
            color: "#ff7f50"
      - name: topology
        kind: Dropdown
        default_value: "full-mesh"
        order_weight: 1400
        choices:
          - name: full-mesh
            label: Full Mesh
          - name: hub-spoke
            label: Hub and Spoke
      - name: status
        kind: Dropdown
        default_value: "draft"
        order_weight: 1500
        choices:
          - name: draft
            label: Draft
            color: "#FFF2CC"
          - name: active
            label: Active
            color: "#A9DFBF"
          - name: decommissioned
            label: Decommissioned
            color: "#D3D3D3"
    relationships:
      - name: tenant
        peer: OrganizationGeneric
        cardinality: one
        optional: false
        kind: Attribute
        order_weight: 1600
      - name: sites
        identifier: sdwan__site
        peer: ServiceSdwanSite
        cardinality: many
        optional: true
        kind: Component
        order_weight: 1700

  - name: SdwanSite
    namespace: Service
    description: A single SD-WAN site (one customer location with one edge device).
    label: SD-WAN Site
    icon: mdi:office-building-marker
    include_in_menu: false
    menu_placement: ServiceSdwan
    human_friendly_id:
      - name__value
    display_label: name__value
    order_by:
      - name__value
    uniqueness_constraints:
      - [sdwan, name__value]
    attributes:
      - name: name
        kind: Text
        order_weight: 1000
      - name: role
        kind: Dropdown
        default_value: "spoke"
        order_weight: 1100
        choices:
          - name: hub
            label: Hub
            color: "#A9CCE3"
          - name: spoke
            label: Spoke
            color: "#CDEACC"
          - name: branch
            label: Branch
            color: "#D2B4DE"
      - name: status
        kind: Dropdown
        default_value: "provisioning"
        order_weight: 1200
        choices:
          - name: provisioning
            label: Provisioning
            color: "#FFF2CC"
          - name: active
            label: Active
            color: "#A9DFBF"
          - name: decommissioned
            label: Decommissioned
            color: "#D3D3D3"
    relationships:
      - name: sdwan
        identifier: sdwan__site
        peer: ServiceSdwan
        cardinality: one
        optional: false
        kind: Parent
        order_weight: 1050
      - name: location
        peer: LocationSite
        cardinality: one
        optional: false
        kind: Attribute
        order_weight: 1300
      - name: lan_subnet
        peer: IpamPrefix
        cardinality: one
        optional: false
        kind: Attribute
        order_weight: 1400
      - name: lan_address
        peer: IpamIPAddress
        cardinality: one
        optional: true
        kind: Attribute
        order_weight: 1500
      - name: sdwan_edge
        peer: DcimDevice
        cardinality: one
        optional: true
        kind: Attribute
        order_weight: 1600
```

- [ ] **Step 2: Validate the schema loads against a running Infrahub**

Run: `set -a; source .env; set +a; uv run infrahubctl schema check schemas/`
Expected: 13+ schemas processed, no errors.

- [ ] **Step 3: Commit**

```bash
git add schemas/sp/service_sdwan.yml
git commit -m "Add ServiceSdwan and ServiceSdwanSite schema"
```

---

## Task 2: Shared bootstrap — manufacturer, platforms, device types

**Files:**
- Modify: `objects/00_manufacturers.yml`
- Modify: `objects/30_platforms.yml`
- Modify: `objects/40_device_types.yml`

- [ ] **Step 1: Add `Versa Networks` manufacturer**

Edit `objects/00_manufacturers.yml`. After the existing `- name: Nokia` entry, add:

```yaml
    - name: Versa Networks
      description: Versa Networks
```

- [ ] **Step 2: Add SD-WAN platforms**

Edit `objects/30_platforms.yml`. Append the two new platform entries inside the same `OrganizationManufacturer`-keyed list of platforms. Match the existing `- name: nokia_sros` block's structure:

```yaml
    - name: cisco_viptela
      description: Cisco SD-WAN (Viptela cEdge / IOS-XE SD-WAN)
      manufacturer: Cisco
      nornir_platform: cisco_xe
      napalm_driver: ios
      netmiko_device_type: cisco_xe
      ansible_network_os: cisco.ios.ios
      # No clab image — SD-WAN edges are not lab-deployable in v1

    - name: versa_flexvnf
      description: Versa Networks VOS (FlexVNF)
      manufacturer: Versa Networks
      nornir_platform: versa_vos
      napalm_driver: ""
      netmiko_device_type: ""
      ansible_network_os: versa.vos.versa
      # No clab image
```

- [ ] **Step 3: Add SD-WAN device types**

Edit `objects/40_device_types.yml`. Inside the existing list, append:

```yaml
    - name: cEdge-1000
      part_number: ISR1100-4G
      manufacturer: Cisco
      description: Cisco SD-WAN cEdge 1000 (4-port branch router)
    - name: FlexVNF-200
      part_number: VFlex-200
      manufacturer: Versa Networks
      description: Versa FlexVNF software branch appliance
```

- [ ] **Step 4: Run yamllint**

Run: `uv run yamllint -s objects/00_manufacturers.yml objects/30_platforms.yml objects/40_device_types.yml`
Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add objects/00_manufacturers.yml objects/30_platforms.yml objects/40_device_types.yml
git commit -m "Add Versa manufacturer, two SD-WAN platforms, two device types"
```

---

## Task 3: Shared bootstrap — pool and groups

**Files:**
- Modify: `objects/50_pools.yml`
- Modify: `objects/55_groups.yml`

- [ ] **Step 1: Add `sdwan_id_pool`**

Edit `objects/50_pools.yml`. After the existing `vpn_id_pool` entry in the `CoreNumberPool` block, add:

```yaml
    - name: sdwan_id_pool
      node: ServiceSdwan
      node_attribute: service_id
      # service_id 100-199 reserved for bootstrap-seeded SD-WAN services.
      # Catalog allocations start at 200.
      start_range: 200
      end_range: 9999
```

- [ ] **Step 2: Add three SD-WAN groups**

Edit `objects/55_groups.yml`. After the existing `l3vpns` group entry, add:

```yaml
    - name: sdwans
      description: All ServiceSdwan rows (target group for the generate_sdwan generator).
    - name: sdwan_viptela_edges
      description: All Cisco Viptela SD-WAN edge devices (artifact target).
    - name: sdwan_versa_edges
      description: All Versa Networks SD-WAN edge devices (artifact target).
```

- [ ] **Step 3: Run yamllint**

Run: `uv run yamllint -s objects/50_pools.yml objects/55_groups.yml`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add objects/50_pools.yml objects/55_groups.yml
git commit -m "Add sdwan_id_pool and three SD-WAN CoreStandardGroups"
```

---

## Task 4: Per-dataset bootstrap — `financial`

**Files:**
- Create: `objects/datasets/financial/90_sdwan.yml`

- [ ] **Step 1: Create the file**

```yaml
---
# Customer-facing prefixes for the Treasury Branch SD-WAN.
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: IpamPrefix
  data:
    - {prefix: 10.250.10.0/24, description: Treasury — London branch LAN, status: active, role: public}
    - {prefix: 10.250.20.0/24, description: Treasury — Frankfurt branch LAN, status: active, role: public}
    - {prefix: 10.250.30.0/24, description: Treasury — Amsterdam branch LAN, status: active, role: public}

---
# Default bootstrap SD-WAN service (hub-spoke, Viptela).
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: ServiceSdwan
  data:
    - name: treasury-branch-sdwan
      description: Treasury Ops branch SD-WAN — hub London + spokes Frankfurt and Amsterdam.
      service_id: 100
      vendor: viptela
      topology: hub-spoke
      status: draft
      tenant:
        kind: OrganizationTenant
        data:
          name: treasury-ops
      member_of_groups:
        - sdwans
      sites:
        kind: ServiceSdwanSite
        data:
          - name: hub-london
            role: hub
            location: lon
            lan_subnet: ["10.250.10.0/24", "default"]
          - name: spoke-frankfurt
            role: spoke
            location: fra
            lan_subnet: ["10.250.20.0/24", "default"]
          - name: spoke-amsterdam
            role: spoke
            location: ams
            lan_subnet: ["10.250.30.0/24", "default"]
```

- [ ] **Step 2: Run yamllint**

Run: `uv run yamllint -s objects/datasets/financial/90_sdwan.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add objects/datasets/financial/90_sdwan.yml
git commit -m "Add default SD-WAN service for the financial dataset"
```

---

## Task 5: Per-dataset bootstrap — `isp`

**Files:**
- Create: `objects/datasets/isp/90_sdwan.yml`

- [ ] **Step 1: Create the file**

```yaml
---
# Customer-facing prefixes for the Flo Streaming overlay.
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: IpamPrefix
  data:
    - {prefix: 10.250.10.0/24, description: Flo Streaming — London PoP LAN, status: active, role: public}
    - {prefix: 10.250.20.0/24, description: Flo Streaming — Frankfurt PoP LAN, status: active, role: public}
    - {prefix: 10.250.30.0/24, description: Flo Streaming — Paris PoP LAN, status: active, role: public}

---
# Default bootstrap SD-WAN service (full-mesh, Viptela).
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: ServiceSdwan
  data:
    - name: flo-streaming-overlay
      description: Flo Streaming any-to-any overlay across three European PoPs.
      service_id: 100
      vendor: viptela
      topology: full-mesh
      status: draft
      tenant:
        kind: OrganizationTenant
        data:
          name: flo-streaming
      member_of_groups:
        - sdwans
      sites:
        kind: ServiceSdwanSite
        data:
          - name: pop-london
            role: spoke
            location: lon
            lan_subnet: ["10.250.10.0/24", "default"]
          - name: pop-frankfurt
            role: spoke
            location: fra
            lan_subnet: ["10.250.20.0/24", "default"]
          - name: pop-paris
            role: spoke
            location: par
            lan_subnet: ["10.250.30.0/24", "default"]
```

- [ ] **Step 2: Run yamllint**

Run: `uv run yamllint -s objects/datasets/isp/90_sdwan.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add objects/datasets/isp/90_sdwan.yml
git commit -m "Add default SD-WAN service for the isp dataset"
```

---

## Task 6: Generator helper — `find_or_create_device`

**Files:**
- Modify: `generators/common.py`

- [ ] **Step 1: Add the helper at the bottom of `generators/common.py`**

Append:

```python
async def find_or_create_device(
    client: InfrahubClient,
    name: str,
    platform_name: str,
    device_type_name: str,
    manufacturer_name: str,
    location_hfid: str,
    role: str,
    branch: str,
) -> Any:
    """Return the DcimDevice with this name, creating it if absent.

    Used by the SD-WAN generator to materialise one edge device per site.
    The device is created with role=cpe, status=active, and bound to the
    site's LocationSite. Idempotent: if a device with this name already
    exists, it is returned unchanged.

    Args:
        client: Active Infrahub SDK client.
        name: Device name (typically ``<service>-<site>-edge``).
        platform_name: HFID of the DcimPlatform (e.g. ``cisco_viptela``).
        device_type_name: HFID of the DcimDeviceType (e.g. ``cEdge-1000``).
        manufacturer_name: HFID of the OrganizationManufacturer.
        location_hfid: HFID of the LocationSite (e.g. ``lon``).
        role: Role enum value (e.g. ``cpe``).
        branch: Branch on which to create.

    Returns:
        The Infrahub node for the device.
    """
    existing = await client.filters(
        kind="DcimDevice", name__value=name, branch=branch
    )
    if existing:
        return existing[0]
    device = await client.create(
        kind="DcimDevice",
        branch=branch,
        name=name,
        role=role,
        status="active",
        platform={"hfid": [platform_name]},
        device_type=[device_type_name, manufacturer_name],
        location={"hfid": [location_hfid]},
    )
    await device.save(allow_upsert=True)
    return device
```

- [ ] **Step 2: Verify mypy still passes**

Run: `uv run mypy generators/common.py`
Expected: `Success: no issues found`.

- [ ] **Step 3: Commit**

```bash
git add generators/common.py
git commit -m "Add find_or_create_device helper for the SD-WAN generator"
```

---

## Task 7: GraphQL query for the SD-WAN generator

**Files:**
- Create: `queries/service/sdwan.gql`

- [ ] **Step 1: Create the query**

```graphql
query Sdwan($name: String!) {
  ServiceSdwan(name__value: $name) {
    edges {
      node {
        id
        name { value }
        service_id { value }
        vendor { value }
        topology { value }
        status { value }
        tenant { node { id name { value } } }
        sites {
          edges {
            node {
              id
              name { value }
              role { value }
              status { value }
              location { node { id name { value } shortname { value } } }
              lan_subnet { node { id prefix { value } } }
              lan_address { node { id address { value } } }
              sdwan_edge { node { id name { value } } }
            }
          }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add queries/service/sdwan.gql
git commit -m "Add GraphQL query feeding the SD-WAN generator"
```

---

## Task 8: SD-WAN generator

**Files:**
- Create: `generators/generate_sdwan.py`

- [ ] **Step 1: Create the generator**

```python
"""SD-WAN generator.

Materialises one edge device per ``ServiceSdwanSite``, allocates a LAN
address for it from the customer's LAN subnet, and adds the device to
the vendor-specific edge group so the artifact pipeline targets it.
Idempotent.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from .common import find_or_create_device

LOG = logging.getLogger(__name__)

# Vendor → (platform name, device-type name, manufacturer name, edge group name)
_VENDOR_TABLE: dict[str, tuple[str, str, str, str]] = {
    "viptela": ("cisco_viptela", "cEdge-1000", "Cisco", "sdwan_viptela_edges"),
    "versa": ("versa_flexvnf", "FlexVNF-200", "Versa Networks", "sdwan_versa_edges"),
}


class SdwanGenerator(InfrahubGenerator):
    """Generator that materialises everything downstream of a ServiceSdwan row."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate edges + LAN IPs for every site of a single SD-WAN service."""
        payload = data or self.data
        svc_edges = payload.get("ServiceSdwan", {}).get("edges", [])
        if not svc_edges:
            LOG.warning("No ServiceSdwan matched; nothing to generate")
            return
        svc = svc_edges[0]["node"]
        vendor = svc["vendor"]["value"]
        if vendor not in _VENDOR_TABLE:
            raise RuntimeError(f"Unknown SD-WAN vendor {vendor!r}")
        platform, device_type, manufacturer, edge_group = _VENDOR_TABLE[vendor]

        group = await self.client.get(
            kind="CoreStandardGroup",
            name__value=edge_group,
            branch=self.branch,
        )

        for site_edge in svc["sites"]["edges"]:
            await self._materialise_site(
                site_edge["node"],
                svc_name=svc["name"]["value"],
                platform=platform,
                device_type=device_type,
                manufacturer=manufacturer,
                edge_group=group,
            )

        svc_obj = await self.client.get(
            kind="ServiceSdwan", id=svc["id"], branch=self.branch
        )
        svc_obj.status.value = "active"  # type: ignore[union-attr]
        await svc_obj.save(allow_upsert=True)

    async def _materialise_site(
        self,
        site: dict[str, Any],
        svc_name: str,
        platform: str,
        device_type: str,
        manufacturer: str,
        edge_group: Any,
    ) -> None:
        """Create edge + LAN IP for one ServiceSdwanSite if not yet materialised."""
        site_obj = await self.client.get(
            kind="ServiceSdwanSite", id=site["id"], branch=self.branch
        )
        location_name = site["location"]["node"]["shortname"]["value"]

        has_edge = site.get("sdwan_edge") and site["sdwan_edge"].get("node")
        if has_edge:
            edge = await self.client.get(
                kind="DcimDevice",
                id=site["sdwan_edge"]["node"]["id"],
                branch=self.branch,
            )
        else:
            edge_name = f"{svc_name}-{site['name']['value']}-edge"
            edge = await find_or_create_device(
                self.client,
                name=edge_name,
                platform_name=platform,
                device_type_name=device_type,
                manufacturer_name=manufacturer,
                location_hfid=location_name,
                role="cpe",
                branch=self.branch,
            )
            site_obj.sdwan_edge = edge

        # Ensure the device is a member of the vendor-specific edge group.
        await edge_group.members.fetch()
        if edge.id not in [p.id for p in edge_group.members.peers]:
            edge_group.members.add([edge.id])
            await edge_group.save(allow_upsert=True)

        has_lan = site.get("lan_address") and site["lan_address"].get("node")
        if not has_lan:
            lan_subnet_str = site["lan_subnet"]["node"]["prefix"]["value"]
            net = ipaddress.IPv4Network(lan_subnet_str)
            lan_ip = await self.client.create(
                kind="IpamIPAddress",
                branch=self.branch,
                address=f"{net.network_address + 1}/{net.prefixlen}",
            )
            await lan_ip.save(allow_upsert=True)
            site_obj.lan_address = lan_ip

        site_obj.status.value = "active"  # type: ignore[union-attr]
        await site_obj.save(allow_upsert=True)
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check generators/generate_sdwan.py && uv run mypy generators/generate_sdwan.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add generators/generate_sdwan.py
git commit -m "Add SD-WAN generator: materialise edges, LAN IPs, group membership"
```

---

## Task 9: GraphQL query for SD-WAN edge transforms

**Files:**
- Create: `queries/config/sdwan_edge.gql`

- [ ] **Step 1: Create the query**

```graphql
query SdwanEdge($device: String!) {
  DcimDevice(name__value: $device) {
    edges {
      node {
        id
        name { value }
        platform { node { name { value } } }
        location { node { name { value } shortname { value } } }
      }
    }
  }
  ServiceSdwanSite(sdwan_edge__name__value: $device) {
    edges {
      node {
        id
        name { value }
        role { value }
        lan_subnet { node { prefix { value } } }
        lan_address { node { address { value } } }
        sdwan {
          node {
            name { value }
            service_id { value }
            vendor { value }
            topology { value }
            tenant { node { name { value } } }
            sites {
              edges {
                node {
                  name { value }
                  location { node { shortname { value } } }
                  lan_subnet { node { prefix { value } } }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add queries/config/sdwan_edge.gql
git commit -m "Add GraphQL query feeding the SD-WAN edge transforms"
```

---

## Task 10: SD-WAN transform fixtures + Viptela test (TDD)

**Files:**
- Modify: `tests/unit/test_transforms/fixtures.py`
- Create: `tests/unit/test_transforms/test_sdwan_viptela.py`

- [ ] **Step 1: Add an `sdwan_edge_data()` helper to `tests/unit/test_transforms/fixtures.py`**

Append at the bottom (mirrors the existing `pe_data()` helper):

```python
def sdwan_edge_data(
    *,
    device_name: str = "treasury-branch-sdwan-hub-london-edge",
    platform: str = "cisco_viptela",
    location: str = "lon",
    site_name: str = "hub-london",
    site_role: str = "hub",
    lan_subnet: str = "10.250.10.0/24",
    lan_address: str = "10.250.10.1/24",
    service_name: str = "treasury-branch-sdwan",
    service_id: int = 100,
    vendor: str = "viptela",
    topology: str = "hub-spoke",
    tenant: str = "treasury-ops",
    sibling_sites: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Build a sample SD-WAN edge transform input payload.

    Args:
        sibling_sites: ``[(name, location_shortname, lan_subnet), ...]``
            entries representing peer sites in the same service.

    Returns:
        Dict shaped like the ``sdwan_edge`` GraphQL query response.
    """
    sibling_sites = sibling_sites or []
    sibling_edges = [
        {
            "node": {
                "name": {"value": sn},
                "location": {"node": {"shortname": {"value": loc}}},
                "lan_subnet": {"node": {"prefix": {"value": lan}}},
            }
        }
        for sn, loc, lan in sibling_sites
    ]
    return {
        "DcimDevice": {
            "edges": [
                {
                    "node": {
                        "id": "edge-id",
                        "name": {"value": device_name},
                        "platform": {"node": {"name": {"value": platform}}},
                        "location": {
                            "node": {
                                "name": {"value": location.upper()},
                                "shortname": {"value": location},
                            }
                        },
                    }
                }
            ]
        },
        "ServiceSdwanSite": {
            "edges": [
                {
                    "node": {
                        "id": "site-id",
                        "name": {"value": site_name},
                        "role": {"value": site_role},
                        "lan_subnet": {"node": {"prefix": {"value": lan_subnet}}},
                        "lan_address": {"node": {"address": {"value": lan_address}}},
                        "sdwan": {
                            "node": {
                                "name": {"value": service_name},
                                "service_id": {"value": service_id},
                                "vendor": {"value": vendor},
                                "topology": {"value": topology},
                                "tenant": {"node": {"name": {"value": tenant}}},
                                "sites": {"edges": sibling_edges},
                            }
                        },
                    }
                }
            ]
        },
    }
```

- [ ] **Step 2: Create the failing Viptela render test**

Create `tests/unit/test_transforms/test_sdwan_viptela.py`:

```python
"""Snapshot-shape tests for the Viptela (Cisco IOS-XE SD-WAN) transform."""

from __future__ import annotations

import asyncio

from transforms.sdwan_viptela import SdwanViptela

from .fixtures import sdwan_edge_data


def _render(data: dict) -> str:
    return asyncio.run(SdwanViptela(data=data).transform(data))


def test_renders_hostname_and_system_block() -> None:
    output = _render(sdwan_edge_data())
    assert "host-name treasury-branch-sdwan-hub-london-edge" in output
    assert "site-id 100" in output


def test_renders_vpn1_lan_address() -> None:
    output = _render(sdwan_edge_data(lan_address="10.250.10.1/24"))
    assert "vpn 1" in output
    assert "ip address 10.250.10.1/24" in output


def test_renders_topology_comment_for_full_mesh() -> None:
    output = _render(sdwan_edge_data(topology="full-mesh"))
    assert "full-mesh" in output


def test_renders_organization_name_from_tenant() -> None:
    output = _render(sdwan_edge_data(tenant="treasury-ops"))
    assert 'organization-name "treasury-ops"' in output
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `uv run pytest tests/unit/test_transforms/test_sdwan_viptela.py -v`
Expected: import error (`transforms.sdwan_viptela` does not exist).

- [ ] **Step 4: Implement the Viptela transform**

Create `transforms/sdwan_viptela.py`:

```python
"""Cisco SD-WAN (Viptela / cEdge) config transform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class SdwanViptela(InfrahubTransform):
    """Render a single Cisco Viptela cEdge config."""

    query = "sdwan_edge"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the Viptela Jinja2 template against the SD-WAN edge query.

        Args:
            data: Result of the ``sdwan_edge`` GraphQL query for one device.

        Returns:
            Rendered Cisco IOS-XE SD-WAN configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("sdwan_viptela.j2")
        return template.render(data=data)
```

Create `templates/sdwan_viptela.j2`:

```jinja
{% set device = data.DcimDevice.edges[0].node %}
{% set site = data.ServiceSdwanSite.edges[0].node %}
{% set svc = site.sdwan.node %}
{% set tenant = svc.tenant.node %}
! Cisco IOS-XE SD-WAN config for {{ device.name.value }}
! topology: {{ svc.topology.value }}
!
system
 host-name        {{ device.name.value }}
 system-ip        10.10.0.{{ svc.service_id.value % 256 }}
 site-id          {{ svc.service_id.value }}
 organization-name "{{ tenant.name.value }}"
!
sdwan
 ! peer sites:
{% for peer_edge in svc.sites.edges %}
 !  - {{ peer_edge.node.name.value }} ({{ peer_edge.node.location.node.shortname.value }}, lan {{ peer_edge.node.lan_subnet.node.prefix.value }})
{% endfor %}
!
vpn 0
 interface ge0/0
  ip address dhcp-client
  tunnel-interface
   encapsulation ipsec
   color biz-internet
!
vpn 1
 interface ge0/1
  ip address {{ site.lan_address.node.address.value }}
  no shutdown
!
end
```

- [ ] **Step 5: Re-run the test, verify it passes**

Run: `uv run pytest tests/unit/test_transforms/test_sdwan_viptela.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_transforms/fixtures.py tests/unit/test_transforms/test_sdwan_viptela.py transforms/sdwan_viptela.py templates/sdwan_viptela.j2
git commit -m "Add Cisco Viptela SD-WAN transform with snapshot tests"
```

---

## Task 11: Versa transform (TDD)

**Files:**
- Create: `tests/unit/test_transforms/test_sdwan_versa.py`
- Create: `transforms/sdwan_versa.py`
- Create: `templates/sdwan_versa.j2`

- [ ] **Step 1: Create the failing Versa test**

```python
"""Snapshot-shape tests for the Versa (VOS) SD-WAN transform."""

from __future__ import annotations

import asyncio

from transforms.sdwan_versa import SdwanVersa

from .fixtures import sdwan_edge_data


def _render(data: dict) -> str:
    return asyncio.run(SdwanVersa(data=data).transform(data))


def test_renders_org_services_block() -> None:
    output = _render(sdwan_edge_data(
        vendor="versa",
        platform="versa_flexvnf",
        tenant="treasury-ops",
    ))
    assert "set orgs org-services treasury-ops" in output


def test_renders_lan_virtual_router() -> None:
    output = _render(sdwan_edge_data(
        vendor="versa",
        platform="versa_flexvnf",
        lan_address="10.250.10.1/24",
    ))
    assert "virtual-router LAN" in output
    assert "10.250.10.1/24" in output


def test_renders_site_id_in_appliance_name() -> None:
    output = _render(sdwan_edge_data(
        vendor="versa",
        platform="versa_flexvnf",
        service_id=100,
    ))
    assert "site-id 100" in output
```

Save as `tests/unit/test_transforms/test_sdwan_versa.py`.

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_transforms/test_sdwan_versa.py -v`
Expected: import error.

- [ ] **Step 3: Implement the Versa transform**

Create `transforms/sdwan_versa.py`:

```python
"""Versa Networks VOS (FlexVNF) SD-WAN config transform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class SdwanVersa(InfrahubTransform):
    """Render a single Versa Networks FlexVNF SD-WAN config."""

    query = "sdwan_edge"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the Versa Jinja2 template against the SD-WAN edge query.

        Args:
            data: Result of the ``sdwan_edge`` GraphQL query for one device.

        Returns:
            Rendered Versa VOS CLI configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("sdwan_versa.j2")
        return template.render(data=data)
```

Create `templates/sdwan_versa.j2`:

```jinja
{% set device = data.DcimDevice.edges[0].node %}
{% set site = data.ServiceSdwanSite.edges[0].node %}
{% set svc = site.sdwan.node %}
{% set tenant = svc.tenant.node %}
# Versa VOS config for {{ device.name.value }}
# topology: {{ svc.topology.value }} / site-id {{ svc.service_id.value }}
set orgs org-services {{ tenant.name.value }} appliance-owner true
set orgs org-services {{ tenant.name.value }} routing-instances default-RI
set orgs org-services {{ tenant.name.value }} virtual-router LAN routing-instance default-RI
set orgs org-services {{ tenant.name.value }} virtual-router LAN interfaces vni-0/1.0 ip-address {{ site.lan_address.node.address.value }}
# peer sites:
{% for peer_edge in svc.sites.edges %}
#  - {{ peer_edge.node.name.value }} ({{ peer_edge.node.location.node.shortname.value }}, lan {{ peer_edge.node.lan_subnet.node.prefix.value }})
{% endfor %}
```

- [ ] **Step 4: Re-run, verify it passes**

Run: `uv run pytest tests/unit/test_transforms/test_sdwan_versa.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_transforms/test_sdwan_versa.py transforms/sdwan_versa.py templates/sdwan_versa.j2
git commit -m "Add Versa Networks SD-WAN transform with snapshot tests"
```

---

## Task 12: Check — `sdwan_id_overlap`

**Files:**
- Create: `queries/validation/sdwan_id_overlap.gql`
- Create: `checks/sdwan_id_overlap.py`

- [ ] **Step 1: Create the query**

`queries/validation/sdwan_id_overlap.gql`:

```graphql
query SdwanIdOverlap {
  ServiceSdwan {
    edges {
      node {
        name { value }
        service_id { value }
      }
    }
  }
}
```

- [ ] **Step 2: Create the check**

`checks/sdwan_id_overlap.py`:

```python
"""Check that no two SD-WAN services share a service_id."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class SdwanIdOverlapCheck(InfrahubCheck):
    """No two ServiceSdwan rows may share a service_id."""

    query = "sdwan_id_overlap"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors when two services collide on service_id.

        Args:
            data: Result of the ``sdwan_id_overlap`` GraphQL query.
        """
        id_to_names: dict[int, list[str]] = defaultdict(list)
        for edge in data.get("ServiceSdwan", {}).get("edges", []):
            node = edge["node"]
            sid_field = node.get("service_id") or {}
            sid = sid_field.get("value")
            if sid is None:
                continue
            id_to_names[int(sid)].append(node["name"]["value"])

        for sid, names in id_to_names.items():
            if len(names) > 1:
                self.log_error(
                    message=f"duplicate service_id {sid}: used by {', '.join(names)}",
                )
```

- [ ] **Step 3: Commit**

```bash
git add queries/validation/sdwan_id_overlap.gql checks/sdwan_id_overlap.py
git commit -m "Add sdwan_id_overlap check"
```

---

## Task 13: Check — `sdwan_site_subnet`

**Files:**
- Create: `queries/validation/sdwan_site_subnet.gql`
- Create: `checks/sdwan_site_subnet.py`

- [ ] **Step 1: Create the query**

`queries/validation/sdwan_site_subnet.gql`:

```graphql
query SdwanSiteSubnet {
  ServiceSdwan {
    edges {
      node {
        name { value }
        sites {
          edges {
            node {
              name { value }
              lan_subnet { node { prefix { value } } }
            }
          }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Create the check**

`checks/sdwan_site_subnet.py`:

```python
"""Check that no two sites of an SD-WAN service have overlapping LAN subnets."""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class SdwanSiteSubnetCheck(InfrahubCheck):
    """Within a ServiceSdwan, all site LAN subnets must be disjoint."""

    query = "sdwan_site_subnet"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors for any intra-service LAN subnet overlap.

        Args:
            data: Result of the ``sdwan_site_subnet`` GraphQL query.
        """
        for svc_edge in data.get("ServiceSdwan", {}).get("edges", []):
            svc = svc_edge["node"]
            subnets: list[tuple[str, ipaddress.IPv4Network]] = []
            for site_edge in svc["sites"]["edges"]:
                site = site_edge["node"]
                subnet_node = (site.get("lan_subnet") or {}).get("node")
                if not subnet_node:
                    continue
                prefix = subnet_node["prefix"]["value"]
                subnets.append((site["name"]["value"], ipaddress.IPv4Network(prefix)))

            for i, (name_a, net_a) in enumerate(subnets):
                for name_b, net_b in subnets[i + 1 :]:
                    if net_a.overlaps(net_b):
                        self.log_error(
                            message=(
                                f"SD-WAN {svc['name']['value']}: "
                                f"{name_a} subnet {net_a} overlaps {name_b} subnet {net_b}"
                            ),
                        )
```

- [ ] **Step 3: Commit**

```bash
git add queries/validation/sdwan_site_subnet.gql checks/sdwan_site_subnet.py
git commit -m "Add sdwan_site_subnet check"
```

---

## Task 14: Register everything in `.infrahub.yml`

**Files:**
- Modify: `.infrahub.yml`

- [ ] **Step 1: Add the new entries**

Edit `.infrahub.yml`. Add to each section:

Under `queries:`:

```yaml
  - {name: sdwan, file_path: queries/service/sdwan.gql}
  - {name: sdwan_edge, file_path: queries/config/sdwan_edge.gql}
  - {name: sdwan_id_overlap, file_path: queries/validation/sdwan_id_overlap.gql}
  - {name: sdwan_site_subnet, file_path: queries/validation/sdwan_site_subnet.gql}
```

Under `python_transforms:`:

```yaml
  - {name: sdwan_viptela, class_name: SdwanViptela, file_path: transforms/sdwan_viptela.py}
  - {name: sdwan_versa, class_name: SdwanVersa, file_path: transforms/sdwan_versa.py}
```

Under `artifact_definitions:` (after the existing PE entries):

```yaml
  - name: sdwan-viptela-config
    artifact_name: sdwan-viptela
    content_type: text/plain
    targets: sdwan_viptela_edges
    transformation: sdwan_viptela
    parameters: {device: name__value}
  - name: sdwan-versa-config
    artifact_name: sdwan-versa
    content_type: text/plain
    targets: sdwan_versa_edges
    transformation: sdwan_versa
    parameters: {device: name__value}
```

Under `generator_definitions:`:

```yaml
  - name: generate_sdwan
    file_path: generators/generate_sdwan.py
    targets: sdwans
    query: sdwan
    class_name: SdwanGenerator
    parameters:
      name: name__value
```

Under `check_definitions:`:

```yaml
  - {name: sdwan_id_overlap, class_name: SdwanIdOverlapCheck, file_path: checks/sdwan_id_overlap.py, targets: sdwans}
  - {name: sdwan_site_subnet, class_name: SdwanSiteSubnetCheck, file_path: checks/sdwan_site_subnet.py, targets: sdwans}
```

- [ ] **Step 2: yamllint**

Run: `uv run yamllint -s .infrahub.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add .infrahub.yml
git commit -m "Register SD-WAN queries, transforms, artifacts, generator, and checks"
```

---

## Task 15: Bootstrap wiring — run the SD-WAN generator

**Files:**
- Modify: `tasks.py`

- [ ] **Step 1: Add the second generator call after the L3VPN one**

Find the line in `tasks.py` that runs the L3VPN generator:

```python
    c.run("uv run python scripts/run_generator.py generate_l3vpn", pty=True)
    _success("Generator complete")
```

Change the `_success("Generator complete")` to immediately follow another `_step` + `c.run` pair:

```python
    c.run("uv run python scripts/run_generator.py generate_l3vpn", pty=True)
    _success("L3VPN generator complete")

    _step("Running the SD-WAN generator")
    c.run("uv run python scripts/run_generator.py generate_sdwan", pty=True)
    _success("SD-WAN generator complete")
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check tasks.py && uv run ruff format --check tasks.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add tasks.py
git commit -m "Run SD-WAN generator in invoke bootstrap"
```

---

## Task 16: Catalog form validator (TDD)

**Files:**
- Modify: `service_catalog/utils/validators.py`
- Modify: `tests/catalog/test_validators.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/catalog/test_validators.py`:

```python
from service_catalog.utils.validators import validate_create_sdwan_form


def _ok_sdwan_sites():
    return [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "spoke-a", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]


def test_sdwan_minimum_two_sites_required() -> None:
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=[_ok_sdwan_sites()[0]],
    )
    assert any("at least two sites" in e.lower() for e in errors)


def test_sdwan_hub_required_when_hub_spoke() -> None:
    sites = [
        {"name": "a", "role": "spoke", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "b", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=sites,
    )
    assert any("hub" in e.lower() for e in errors)


def test_sdwan_unique_site_names_required() -> None:
    sites = [
        {"name": "dup", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "dup", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=sites,
    )
    assert any("unique" in e.lower() and "name" in e.lower() for e in errors)


def test_sdwan_unique_location_required() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "spoke", "role": "spoke", "location": "lon", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=sites,
    )
    assert any("location" in e.lower() for e in errors)


def test_sdwan_overlapping_lan_subnets() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.0.0/16"},
        {"name": "spoke", "role": "spoke", "location": "fra", "lan_subnet": "10.250.10.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=sites,
    )
    assert any("overlap" in e.lower() for e in errors)


def test_sdwan_garbage_cidr_caught() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "not-a-cidr"},
        {"name": "spoke", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=sites,
    )
    assert any("valid" in e.lower() and "cidr" in e.lower() for e in errors)


def test_sdwan_happy_path() -> None:
    errors = validate_create_sdwan_form(
        name="x", tenant="t", vendor="viptela", topology="hub-spoke", sites=_ok_sdwan_sites(),
    )
    assert errors == []
```

- [ ] **Step 2: Run, verify failures**

Run: `uv run pytest tests/catalog/test_validators.py -v -k sdwan`
Expected: 7 `ImportError`s — the validator doesn't exist yet.

- [ ] **Step 3: Implement the validator**

Append to `service_catalog/utils/validators.py`:

```python
import ipaddress
from typing import Any


def validate_create_sdwan_form(
    *,
    name: str,
    tenant: str,
    vendor: str,
    topology: str,
    sites: list[dict[str, Any]],
) -> list[str]:
    """Return a list of human-readable form errors (empty on success).

    Args:
        name: Service name.
        tenant: Tenant HFID.
        vendor: ``viptela`` or ``versa``.
        topology: ``hub-spoke`` or ``full-mesh``.
        sites: List of dicts with ``name``, ``role``, ``location``, ``lan_subnet``.

    Returns:
        Error strings ready to show in the Streamlit UI.
    """
    errors: list[str] = []

    if not name.strip():
        errors.append("Name is required.")
    if not tenant:
        errors.append("Tenant is required.")
    if vendor not in {"viptela", "versa"}:
        errors.append(f"Vendor must be 'viptela' or 'versa' (got {vendor!r}).")
    if topology not in {"hub-spoke", "full-mesh"}:
        errors.append(f"Topology must be 'hub-spoke' or 'full-mesh' (got {topology!r}).")
    if len(sites) < 2:
        errors.append("An SD-WAN service needs at least two sites.")

    if topology == "hub-spoke":
        hubs = [s for s in sites if s.get("role") == "hub"]
        if len(hubs) != 1:
            errors.append("hub-spoke topology must have exactly one site with role 'hub'.")

    names = [s.get("name", "") for s in sites]
    if len(set(n for n in names if n)) != len([n for n in names if n]):
        errors.append("Site names must be unique within the service.")

    locations = [s.get("location", "") for s in sites]
    if len(set(loc for loc in locations if loc)) != len([loc for loc in locations if loc]):
        errors.append("Each site must use a unique location.")

    parsed: list[tuple[str, ipaddress.IPv4Network]] = []
    for s in sites:
        cidr = s.get("lan_subnet", "")
        if not cidr:
            continue
        try:
            parsed.append((s.get("name", "?"), ipaddress.IPv4Network(cidr, strict=False)))
        except ValueError:
            errors.append(f"{s.get('name', '?')}: {cidr!r} is not a valid CIDR.")

    for i, (name_a, net_a) in enumerate(parsed):
        for name_b, net_b in parsed[i + 1 :]:
            if net_a.overlaps(net_b):
                errors.append(f"{name_a} subnet {net_a} overlaps {name_b} subnet {net_b}.")

    return errors
```

- [ ] **Step 4: Run, verify all pass**

Run: `uv run pytest tests/catalog/test_validators.py -v -k sdwan`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add service_catalog/utils/validators.py tests/catalog/test_validators.py
git commit -m "Add SD-WAN catalog form validator with tests"
```

---

## Task 17: Catalog page — Create SD-WAN

**Files:**
- Create: `service_catalog/pages/2_Create_SDWAN.py`

- [ ] **Step 1: Create the page**

```python
"""Create SD-WAN service wizard form."""

from __future__ import annotations

import os
import time
import urllib.request
import uuid
from typing import Any

import streamlit as st
from utils import client_for, run_async
from utils.validators import validate_create_sdwan_form

st.title("Create SD-WAN service")

client_main = client_for()
tenants = run_async(client_main.all(kind="OrganizationTenant"))
tenant_names = sorted(t.name.value for t in tenants)

locations = run_async(client_main.all(kind="LocationSite"))
location_options = {loc.name.value: loc.shortname.value for loc in locations}

with st.form("create_sdwan"):
    st.subheader("Service basics")
    name = st.text_input("Name", placeholder="acme-overlay")
    description = st.text_input("Description (optional)")
    tenant = st.selectbox("Tenant", options=tenant_names)
    vendor = st.radio("Vendor", options=["viptela", "versa"], horizontal=True)
    topology = st.radio("Topology", options=["full-mesh", "hub-spoke"], horizontal=True)

    st.subheader("Sites")
    site_count = st.number_input("Number of sites", min_value=2, max_value=6, value=2, step=1)
    sites: list[dict[str, Any]] = []
    for i in range(int(site_count)):
        st.markdown(f"**Site {i + 1}**")
        site_name = st.text_input("Site name", key=f"sn_{i}")
        role = st.radio(
            "Role",
            options=["hub", "spoke", "branch"],
            key=f"sr_{i}",
            horizontal=True,
        )
        location_label = st.selectbox(
            "Location", options=list(location_options.keys()), key=f"sloc_{i}"
        )
        lan_subnet = st.text_input(
            "LAN subnet (CIDR)", key=f"slan_{i}", placeholder="10.250.10.0/24"
        )
        sites.append(
            {
                "name": site_name,
                "role": role,
                "location": location_options[location_label],
                "lan_subnet": lan_subnet,
            }
        )

    submitted = st.form_submit_button("Create SD-WAN service", type="primary")

if submitted:
    errors = validate_create_sdwan_form(
        name=name, tenant=tenant, vendor=vendor, topology=topology, sites=sites,
    )
    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    with st.spinner("Opening branch and creating objects..."):
        branch_name = f"sdwan/{uuid.uuid4().hex[:8]}"
        branch = run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        sdwan_id_pool = run_async(client.get(kind="CoreNumberPool", name__value="sdwan_id_pool"))
        sdwans_group = run_async(client.get(kind="CoreStandardGroup", name__value="sdwans"))

        svc = run_async(
            client.create(
                kind="ServiceSdwan",
                name=name,
                description=description,
                service_id=sdwan_id_pool,
                vendor=vendor,
                topology=topology,
                tenant={"hfid": [tenant]},
                member_of_groups=[sdwans_group.id],
            )
        )
        run_async(svc.save())
        service_id = int(svc.service_id.value)

        for s in sites:
            lan = run_async(
                client.create(
                    kind="IpamPrefix",
                    prefix=s["lan_subnet"],
                    status="active",
                    role="public",
                )
            )
            run_async(lan.save())
            site_obj = run_async(
                client.create(
                    kind="ServiceSdwanSite",
                    name=s["name"],
                    sdwan=svc,
                    role=s["role"],
                    location={"hfid": [s["location"]]},
                    lan_subnet=lan,
                )
            )
            run_async(site_obj.save())

        # Wait for the generator (auto-fired by group membership) to flip
        # the service to `active` before triggering artifact regeneration.
        def _is_active() -> bool:
            v = run_async(client.get(kind="ServiceSdwan", name__value=name))
            return v.status.value == "active"

        deadline = time.monotonic() + 120
        while not _is_active() and time.monotonic() < deadline:
            time.sleep(2)

        # Trigger artifact regeneration on the branch so the PC shows real
        # per-edge config diffs.
        for definition in run_async(client.all(kind="CoreArtifactDefinition")):
            url = f"{client.address}/api/artifact/generate/{definition.id}?branch={branch_name}"
            request = urllib.request.Request(
                url,
                method="POST",
                headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
            )
            urllib.request.urlopen(request).read()

        pc = run_async(
            client_main.create(
                kind="CoreProposedChange",
                source_branch=branch_name,
                destination_branch="main",
                name=f"Create SD-WAN {name}",
            )
        )
        run_async(pc.save())

    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000")
    st.success(f"Branch `{branch_name}` opened, service_id={service_id}.")
    st.markdown(
        f"**Next step:** review the diff and the validation pipeline in Infrahub, "
        f"then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check service_catalog/pages/2_Create_SDWAN.py && uv run ruff format --check service_catalog/pages/2_Create_SDWAN.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add service_catalog/pages/2_Create_SDWAN.py
git commit -m "Add Create SD-WAN service catalog page"
```

---

## Task 18: Dashboard update — list SD-WAN services

**Files:**
- Modify: `service_catalog/pages/0_Dashboard.py`

- [ ] **Step 1: After the existing L3VPN table rendering, append:**

Read the current end of `service_catalog/pages/0_Dashboard.py` to find where the existing L3VPN table is rendered. After the L3VPN DataFrame render (likely `st.dataframe(df)` near the end), add:

```python
st.markdown("---")
st.subheader("SD-WAN services")
sdwans = run_async(client.all(kind="ServiceSdwan", branch=branch, prefetch_relationships=True))
if not sdwans:
    st.info("No SD-WAN services yet. Use **Create SD-WAN service** to define your first.")
else:
    sdwan_rows = []
    for svc in sdwans:
        sites = run_async(
            client.filters(kind="ServiceSdwanSite", sdwan__ids=[svc.id], branch=branch)
        )
        sdwan_rows.append(
            {
                "name": svc.name.value,
                "tenant": svc.tenant.peer.name.value if svc.tenant and svc.tenant.peer else "",
                "vendor": svc.vendor.value,
                "topology": svc.topology.value,
                "service_id": svc.service_id.value,
                "# sites": len(sites),
                "status": svc.status.value,
            }
        )
    st.dataframe(pd.DataFrame(sdwan_rows), use_container_width=True)
```

If `pd` isn't already imported in this file, add `import pandas as pd` at the top alongside the existing imports.

- [ ] **Step 2: Lint**

Run: `uv run ruff check service_catalog/pages/0_Dashboard.py && uv run ruff format --check service_catalog/pages/0_Dashboard.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add service_catalog/pages/0_Dashboard.py
git commit -m "Dashboard: list SD-WAN services in a second table"
```

---

## Task 19: Menu update — sidebar entry for SD-WAN

**Files:**
- Modify: `menus/menu.yml`

- [ ] **Step 1: Inspect current Service Catalog section**

Read `menus/menu.yml`. Find the existing **Service Catalog → L3 VPNs** entry (look for `kind: ServiceL3Vpn` under a Service-Catalog-level group).

- [ ] **Step 2: Add an SD-WAN sibling entry**

After the L3 VPNs menu entry under the same parent, insert:

```yaml
    - name: SD-WAN
      kind: ServiceSdwan
      icon: mdi:lan-connect
      order_weight: 1200
```

(Use whatever the next free `order_weight` is — the existing L3 VPNs entry's order_weight is the reference.)

- [ ] **Step 3: Run yamllint**

Run: `uv run yamllint -s menus/menu.yml`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add menus/menu.yml
git commit -m "menu: add SD-WAN entry under Service Catalog"
```

---

## Task 20: Docs — new service page

**Files:**
- Create: `docs/docs/services/sdwan.mdx`

- [ ] **Step 1: Create the page**

```markdown
---
title: SD-WAN service
---

The SD-WAN service models a customer's SD-WAN overlay across one or more sites. Each site has a dedicated edge device (per-site `DcimDevice` created by the generator). The default vendor is Cisco Viptela (cEdge / IOS-XE SD-WAN); Versa Networks VOS is available as an alternate.

## Schema

See [Schema reference](../schema-reference.mdx#servicesdwan) for the full field tables. The shape parallels `ServiceL3Vpn`:

- `ServiceSdwan` — name, `service_id`, `vendor`, `topology`, tenant, sites.
- `ServiceSdwanSite` — name, `role`, `location`, `lan_subnet`, `lan_address`, `sdwan_edge`.

## Lifecycle

1. **Catalog submit** opens a branch and creates `ServiceSdwan` + N × `ServiceSdwanSite`. The service joins the `sdwans` `CoreStandardGroup`.
2. **`generate_sdwan` generator** fires automatically on the branch. For each site, it creates a `DcimDevice` (`<service>-<site>-edge`) with the vendor-appropriate platform / device_type / manufacturer, adds the device to `sdwan_viptela_edges` or `sdwan_versa_edges`, allocates a LAN address inside the customer-supplied LAN subnet, binds `site.sdwan_edge` and `site.lan_address`, and flips status to `active`. Service status flips to `active` once every site is materialized.
3. **Artifact regeneration** on the branch renders one config per edge via the matching transform.
4. **Proposed change** opens against `main`; the configuration validator shows per-edge diffs.

## Vendor differences

| Aspect | Viptela (cEdge) | Versa (FlexVNF) |
|---|---|---|
| Platform | `cisco_viptela` | `versa_flexvnf` |
| Device type | `cEdge-1000` | `FlexVNF-200` |
| Edge group | `sdwan_viptela_edges` | `sdwan_versa_edges` |
| Artifact definition | `sdwan-viptela-config` | `sdwan-versa-config` |
| Config flavor | IOS-XE SD-WAN CLI (system / sdwan / vpn N) | Versa VOS CLI (set orgs org-services …) |

## Checks

- `sdwan_id_overlap` — no two services share `service_id`. Safety net behind the pool.
- `sdwan_site_subnet` — no two sites within the same service have overlapping LAN subnets.

## Known gaps

- **No SD-WAN controllers** are modelled — vManage / vSmart / vBond and Versa Director / Analytics are out of scope for v1.
- **No transport circuits** — edges have only a LAN-side address; no MPLS-vs-Internet-vs-LTE distinction.
- **No overlay tunnels or BGP** in the rendered config — templates emit the intent (system identity, vpn 1 LAN block, organization name) but the peer list is comments only.
- **No containerlab support** — the `clab-mpls-topology` artifact stays MPLS-only. Adding SD-WAN edges would require a `data.ServiceSdwanSite.edges` loop in the clab template and a new `linux` CE per SD-WAN site.
```

- [ ] **Step 2: Commit**

```bash
git add docs/docs/services/sdwan.mdx
git commit -m "docs: add SD-WAN service page"
```

---

## Task 21: Docs — schema reference updates

**Files:**
- Modify: `docs/docs/schema-reference.mdx`

- [ ] **Step 1: Add an SD-WAN section near the existing L3VPN sections**

After the `ServiceL3VpnSite` section (and before `TopologyMplsBackbone`), insert two new subsections paralleling the L3VPN ones. Use the field tables from the design doc (`docs/superpowers/specs/2026-05-17-sdwan-service-design.md` Section 3.3) as the source of truth.

- [ ] **Step 2: Add `sdwan_id_pool` to the Resource Pools table**

Under the "Resource Pools" section, add a row:

```markdown
| `sdwan_id_pool` | `CoreNumberPool` | Globally unique SD-WAN service IDs | Integer (range 200–9999; values 100–199 are reserved for bootstrap-seeded SD-WANs) |
```

- [ ] **Step 3: Vale + commit**

Run: `vale $(find ./docs -type f \( -name "*.mdx" -o -name "*.md" \) -not -path "./docs/superpowers/*")`
Expected: 0 errors. (Warnings are fine.)

```bash
git add docs/docs/schema-reference.mdx
git commit -m "docs: schema-reference covers ServiceSdwan + ServiceSdwanSite + sdwan_id_pool"
```

---

## Task 22: Docs — quickstart + sidebar wiring

**Files:**
- Modify: `docs/docs/quickstart.mdx`
- Modify: `docs/sidebars.ts`

- [ ] **Step 1: Append a paragraph at the end of quickstart step 4**

Find step 4 in `docs/docs/quickstart.mdx` (the Streamlit Service Catalog step). Append at the end:

```markdown
The Service Catalog also exposes a second wizard — **Create SD-WAN service** — for provisioning Cisco Viptela or Versa Networks SD-WAN overlays. The flow mirrors Create L3VPN: pick vendor and topology, list the sites, submit. See [services/sdwan](./services/sdwan.mdx) for the lifecycle, vendor differences, and known gaps.
```

- [ ] **Step 2: Link the new page in the sidebar**

Read `docs/sidebars.ts`. Find the entry for `services/l3vpn` and add `services/sdwan` immediately after it:

```ts
        'services/l3vpn',
        'services/sdwan',
```

- [ ] **Step 3: Build the docs site to verify**

Run: `cd docs && pnpm install --frozen-lockfile && pnpm run build && cd ..`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add docs/docs/quickstart.mdx docs/sidebars.ts
git commit -m "docs: quickstart + sidebar mention SD-WAN service"
```

---

## Task 23: End-to-end smoke

**No file changes** — this is a verification gate, not new code.

- [ ] **Step 1: Confirm working tree is clean**

Run: `git status`
Expected: nothing to commit.

- [ ] **Step 2: Run the full lint suite**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy . && uv run yamllint -s .`
Expected: every command exits 0.

- [ ] **Step 3: Run the unit + catalog tests**

Run: `uv run pytest tests/unit/ tests/catalog/ -v`
Expected: every test passes, including the new `test_sdwan_viptela`, `test_sdwan_versa`, and `test_sdwan_*` validator tests.

- [ ] **Step 4: End-to-end init from the canonical clone**

Run (from `~/src/infrahub-demo-sp`):

```bash
set -a; source .env; set +a
uv run invoke init   # default dataset: financial
```

Expected: bootstrap completes; the `Running the SD-WAN generator` line shows `✓ SD-WAN generator complete`; the final summary banner shows "All N artifacts Ready" where N is the original 6 plus the per-edge SD-WAN configs (3 for hub-spoke financial).

- [ ] **Step 5: Inspect live state**

Run:

```bash
uv run python - <<'PY'
from infrahub_sdk import InfrahubClientSync
c = InfrahubClientSync()
svc = c.get(kind="ServiceSdwan", name__value="treasury-branch-sdwan")
print(f"Service: {svc.name.value}  status={svc.status.value}  vendor={svc.vendor.value}")
sites = c.all(kind="ServiceSdwanSite")
print(f"Sites: {[(s.name.value, s.status.value) for s in sites]}")
arts = [a for a in c.all(kind="CoreArtifact") if a.name.value.startswith("sdwan-")]
print(f"SD-WAN artifacts: {[(a.name.value, a.status.value) for a in arts]}")
PY
```

Expected: service is `active`, every site is `active`, every SD-WAN artifact is `Ready`.

- [ ] **Step 6: Repeat for the `isp` dataset**

Edit `~/src/infrahub-demo-sp/.env` and set `INFRAHUB_DATASET="isp"`. Re-run:

```bash
set -a; source .env; set +a
uv run invoke init
```

Expected: same outcome, but `flo-streaming-overlay` materializes 3 PoP sites in full-mesh topology.

- [ ] **Step 7: Catalog smoke**

Open http://localhost:8501 → **Create SD-WAN service**. Fill in:
- Name: `pete-test-sdwan`
- Tenant: any
- Vendor: `versa` (to exercise the alternate path)
- Topology: `full-mesh`
- 2 sites at distinct locations with non-overlapping `10.250.40.0/24` and `10.250.50.0/24` LANs

Submit. Expected: branch opens, PC link appears. Open the PC; verify the `sdwan-versa-config` artifact validator shows two new edge configs (one per site) and that the L3VPN / clab artifacts are untouched.

---

## Self-Review

**Spec coverage**

- [Section 3.3 schema] → Task 1.
- [Section 3.4 generator] → Tasks 6, 7, 8.
- [Section 3.5 transforms + templates] → Tasks 9, 10, 11.
- [Section 3.6 checks] → Tasks 12, 13.
- [Section 4 bootstrap data] → Tasks 2, 3, 4, 5.
- [Section 4.1 shared bootstrap] → Tasks 2, 3.
- [Section 4.2 per-dataset] → Tasks 4, 5.
- [Section 5 catalog] → Tasks 16, 17, 18, 19.
- [Section 6 bootstrap wiring] → Tasks 14, 15.
- [Section 7 tests] → Tasks 10, 11, 16 (covers transforms + validators).
- [Section 8 docs] → Tasks 20, 21, 22.
- Smoke / verification → Task 23.

**Placeholder scan:** every code block contains real content. No TBDs, no "implement later", no "similar to". Imports, helper names, and method signatures used in later tasks match the definitions in earlier tasks.

**Type consistency:**
- `SdwanGenerator` (Task 8) referenced by `.infrahub.yml` (Task 14) — match.
- `SdwanViptela` / `SdwanVersa` class names (Tasks 10, 11) referenced by `.infrahub.yml` (Task 14) — match.
- `SdwanIdOverlapCheck` / `SdwanSiteSubnetCheck` (Tasks 12, 13) referenced by `.infrahub.yml` (Task 14) — match.
- `find_or_create_device` (Task 6) called from Task 8 — match.
- `validate_create_sdwan_form` (Task 16) imported by the catalog page (Task 17) — match.
- Fixture function `sdwan_edge_data` (Task 10) used by both Viptela (Task 10) and Versa (Task 11) tests — match.

No gaps identified.
