# Infrahub SP Demo — MPLS L3VPN

An [Infrahub](https://github.com/opsmill/infrahub) demonstration repository
that models a multi-vendor MPLS backbone and provisions L3VPNs as a service
through a Streamlit Service Catalog.

The demo covers four vendor platforms — **Arista EOS**, **Cisco IOS-XR**,
**Juniper Junos**, and **Nokia SR OS** — connected in a full-mesh iBGP
backbone over ISIS L2 + LDP. L3VPN creation drives an Infrahub generator
that materialises VRFs, route targets, PE-CE interfaces, IP addresses, and
eBGP sessions, then renders per-vendor device configs as Infrahub artifacts.

---

## What's included

| Component | Details |
|---|---|
| **Schemas** | SP-specific nodes: `ServiceL3Vpn`, `ServiceL3VpnSite`, `TopologyMplsBackbone`, `MplsIsisProcess`, `MplsLdpProcess`, `MplsBgpProcess` |
| **Bootstrap data** | 4 PE routers, 6 backbone p2p links, ISIS/LDP/MP-BGP processes, full-mesh iBGP sessions, resource pools, tenants |
| **Generator** | `L3VpnGenerator` — materialises VRF + RD/RT, allocates PE-CE /30, creates eBGP session |
| **Transforms** | One Python + Jinja2 transform per vendor, plus a containerlab topology transform |
| **Checks** | `l3vpn_overlap`, `l3vpn_site_subnet`, `pe_interface_alloc`, `backbone_session_count` |
| **Service Catalog** | Streamlit app — Dashboard and Create L3VPN pages |
| **Lab** | Optional containerlab artifact (Arista cEOS + Nokia SR Linux + Linux CEs) |

---

## Quick start

### Prerequisites

- Docker / Docker Compose
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)

### 1. Clone and install

```bash
git clone https://github.com/opsmill/sp-demo-mpls-l3vpn.git
cd sp-demo-mpls-l3vpn
cp .env.example .env
uv sync
```

### 2. Start Infrahub and load bootstrap data

`uv run` does not auto-load `.env`, so export it into the shell first:

```bash
set -a; source .env; set +a
uv run invoke init
```

(Or use any equivalent — `direnv`, a shell rc snippet, etc.)

This command:

- Destroys any prior Infrahub state
- Starts the Infrahub containers (Docker Compose)
- Loads schemas in three passes (base → extensions → SP)
- Loads the sidebar menu
- Loads all bootstrap objects (PEs, backbone, pools, tenants, groups)

Wait about 30 seconds after the containers start before the bootstrap runs.

### 3. Open the Infrahub UI

Visit **http://localhost:8000** and log in with `admin` / `infrahub`.

The sidebar shows:

- **Service Catalog → L3 VPNs** — create and manage L3VPNs
- **Topology → MPLS Backbones** — backbone overview
- **MPLS** — ISIS, LDP, and MP-BGP processes per PE

### 4. Start the Streamlit Service Catalog

```bash
uv run invoke start --catalog --build
```

Visit **http://localhost:8501** to create your first L3VPN. The catalog
allocates a VPN ID, opens a feature branch, creates the service objects,
and opens a Proposed Change — all in one click.

### 5. Review and merge the Proposed Change

Back in the Infrahub UI, navigate to **Proposed Changes**. You should see
the new change with:

- Generator output (VRF, IPs, eBGP session)
- Check results (all four checks must pass)
- Config artifacts for each PE

Merge the Proposed Change to promote the L3VPN to `active`.

---

## Invoke tasks

```bash
uv run invoke --list
```

| Task | Description |
|---|---|
| `invoke init` | Destroy → start → bootstrap (full reset) |
| `invoke start` | Start Infrahub containers |
| `invoke start --catalog --build` | Start with Streamlit sidecar |
| `invoke destroy` | Stop and remove containers + volumes |
| `invoke bootstrap` | Load schemas, menu, and bootstrap objects |
| `invoke lint` | Run ruff, mypy, yamllint |
| `invoke test` | Run unit tests |
| `invoke test --kind integration` | Run integration tests (needs running Infrahub) |
| `invoke lab.deploy` | Fetch clab artifact + deploy containerlab |
| `invoke lab.destroy` | Tear down containerlab |
| `invoke lab.push-arista` | Push Arista EOS config from Infrahub to cEOS |
| `invoke lab.status` | Show container status |

---

## Documentation

| Page | Contents |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | Prerequisites, step-by-step setup, first L3VPN |
| [docs/architecture.md](docs/architecture.md) | Data flow, directory map, schema layering |
| [docs/schema-reference.md](docs/schema-reference.md) | SP schema field tables with user-provided vs generator-filled annotations |
| [docs/services/l3vpn.md](docs/services/l3vpn.md) | L3VPN service lifecycle, checks, per-vendor config diffs |
| [docs/lab/containerlab.md](docs/lab/containerlab.md) | Lab setup, SR OS → SR Linux substitution, config push, known gaps |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Bootstrap timeouts, missing free interface, port conflicts, image pulls |
| [docs/AGENTS.md](docs/AGENTS.md) | Guidance for AI coding assistants working in this repo |

---

## Repository layout

```
sp-demo-mpls-l3vpn/
├── schemas/
│   ├── base/           # Core schema-library nodes
│   ├── extensions/     # Routing, VRF, topology extensions
│   └── sp/             # SP-demo-specific schemas
├── objects/            # Bootstrap YAML objects
├── generators/         # L3VPN generator (Python)
├── transforms/         # Per-vendor + clab transforms (Python)
├── templates/          # Jinja2 templates
├── checks/             # Four pipeline checks (Python)
├── queries/            # GraphQL queries
├── menus/              # Sidebar menu YAML
├── service_catalog/    # Streamlit application
├── tests/              # Unit and integration tests
├── lab/                # Runtime only — not committed
├── tasks.py            # Invoke task definitions
├── .infrahub.yml       # Infrahub component registry
└── pyproject.toml      # Python project config
```

---

## Development

```bash
# Lint
uv run invoke lint

# Unit tests
uv run pytest tests/unit/

# Coverage report
uv run pytest --cov --cov-report=html tests/

# YAML lint
uv run yamllint .
```

All Python code requires type hints on every function signature, Google-style
docstrings on all modules and classes, and must pass `ruff` + `mypy`.

---

## Tech stack

- Python 3.10–3.12, managed with [`uv`](https://docs.astral.sh/uv/)
- [Infrahub SDK](https://docs.infrahub.app/python-sdk/) >= 1.15.1
- Jinja2 for config templating
- Streamlit for the service catalog
- pytest + ruff + mypy for quality gates
- containerlab for lab emulation (optional)

---

## License

MIT — see [LICENSE.txt](LICENSE.txt).
