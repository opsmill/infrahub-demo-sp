# infrahub-demo-sp

An [Infrahub](https://github.com/opsmill/infrahub) example repository that
models a multi-vendor MPLS backbone (Arista EOS, Cisco IOS-XR, Juniper Junos,
Nokia SR OS) and provisions L3VPNs as a service through a Streamlit Service
Catalog.

**Full documentation:** <https://docs.infrahub.app/infrahub-demo-sp>

---

## Quick start

```bash
git clone https://github.com/opsmill/infrahub-demo-sp.git
cd infrahub-demo-sp
cp .env.example .env
set -a; source .env; set +a
uv sync
uv run invoke init
```

`set -a` is required: `.env` holds quoted assignments, so a bare `source .env`
leaves them as shell variables that `invoke` and `infrahubctl` never see, and
`invoke init` fails at "Loading schemas" with `Authentication is required`.

Visit <http://localhost:8000> (admin / infrahub) for the Infrahub UI. To
enable the Streamlit Service Catalog sidecar set
`INFRAHUB_SERVICE_CATALOG="true"` in `.env` and re-run `uv run invoke start`.
The sidecar runs the published image, so there is nothing to build.

See the [quickstart guide](https://docs.infrahub.app/infrahub-demo-sp/quickstart) for
the step-by-step walkthrough.

## What's inside

- **Service Catalog** (Streamlit, port 8501): wizards for creating L3VPNs
  and SD-WAN services, an **Add PE** page that onboards a PE router to the
  backbone as a proposed change, a Dashboard view of existing services, and a
  **Batfish Check** page that runs static config validation against the
  rendered MPLS backbone and shows findings per query.
- **Per-vendor config rendering**: one transform + Jinja2 template per
  vendor (Arista EOS, Cisco IOS-XR, Juniper Junos, Nokia SR OS), plus
  SR Linux as the clab substitute for the Nokia PE.
- **Five proposed-change checks** including `BatfishBackboneCheck` for
  Batfish-driven static validation of the rendered configs.
- **Add a PE router from the command line**: `invoke demo-add-device` runs
  the Add PE flow without the browser. It opens a branch, has the generator
  build the router and its iBGP sessions, opens a proposed change and prints
  its link.
- **Containerlab integration**: `invoke lab.deploy` builds a topology
  artifact, fetches per-PE startup configs, and boots cEOS + SR Linux
  in containerlab for end-to-end testing. `--branch <name>` boots a proposed
  change before it is merged.

Quick task reference (run via `uv run invoke <name>`):

| Task | Purpose |
|---|---|
| `init` | Destroy + start + bootstrap end-to-end |
| `start` / `destroy` | Bring Infrahub up / tear it down |
| `bootstrap` | Load schemas + data into a running Infrahub |
| `demo-add-device` | Add a PE router on a new branch and open a proposed change; prints its link |
| `batfish` | Run `BatfishBackboneCheck` against the live MPLS backbone |
| `lab.deploy` / `lab.destroy` | Bring the containerlab up / down (`lab.deploy --branch <name>` boots a branch) |
| `lab.push-arista` | Push the rendered Arista config to the lab cEOS node |
| `lint` / `test` | Ruff + mypy + yamllint / pytest |

Run `uv run invoke list` for the full list with descriptions.

## Container images

CI publishes two images to the OpsMill registry, for `linux/amd64` and
`linux/arm64`:

| Image | Content |
|---|---|
| `registry.opsmill.io/opsmill/infrahub-demo-sp-streamlit` | The Streamlit Service Catalog |
| `registry.opsmill.io/opsmill/infrahub-demo-sp-batfish-runner` | The Batfish runner sidecar |

Pull the image with the `main` tag for the latest published state, or a
`sha-<commit>` tag to pin an exact build. See the
[quickstart](https://docs.infrahub.app/infrahub-demo-sp/quickstart) for how to
run them and how to build them from a checkout.
