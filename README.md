# infrahub-demo-sp

An [Infrahub](https://github.com/opsmill/infrahub) example repository that
models a multi-vendor MPLS backbone (Arista EOS, Cisco IOS-XR, Juniper Junos,
Nokia SR OS) and provisions L3VPNs as a service through a Streamlit Service
Catalog.

**Full documentation:** <https://docs.infrahub.app/demo-sp/>

---

## Quick start

```bash
git clone https://github.com/opsmill/infrahub-demo-sp.git
cd infrahub-demo-sp
cp .env.example .env
source .env
uv sync
uv run invoke init
```

Visit <http://localhost:8000> (admin / infrahub) for the Infrahub UI. To
enable the Streamlit Service Catalog sidecar set
`INFRAHUB_SERVICE_CATALOG="true"` in `.env` and re-run `uv run invoke start`.

See the [quickstart guide](https://docs.infrahub.app/demo-sp/quickstart) for
the step-by-step walkthrough.

## What's inside

- **Service Catalog** (Streamlit, port 8501): wizards for creating L3VPNs
  and SD-WAN services, a Dashboard view of existing services, and a
  **Batfish Check** page that runs static config validation against the
  rendered MPLS backbone and shows findings per query.
- **Per-vendor config rendering**: one transform + Jinja2 template per
  vendor (Arista EOS, Cisco IOS-XR, Juniper Junos, Nokia SR OS), plus
  SR Linux as the clab substitute for the Nokia PE.
- **Five proposed-change checks** including `BatfishBackboneCheck` for
  Batfish-driven static validation of the rendered configs.
- **Containerlab integration**: `invoke lab.deploy` builds a topology
  artifact, fetches per-PE startup configs, and boots cEOS + SR Linux
  in containerlab for end-to-end testing.

Quick task reference (run via `uv run invoke <name>`):

| Task | Purpose |
|---|---|
| `init` | Destroy + start + bootstrap end-to-end |
| `start` / `destroy` | Bring Infrahub up / tear it down |
| `bootstrap` | Load schemas + data into a running Infrahub |
| `batfish` | Run `BatfishBackboneCheck` against the live MPLS backbone |
| `lab.deploy` / `lab.destroy` | Bring the containerlab up / down |
| `lab.push-arista` | Push the rendered Arista config to the lab cEOS node |
| `lint` / `test` | Ruff + mypy + yamllint / pytest |

Run `uv run invoke list` for the full list with descriptions.
