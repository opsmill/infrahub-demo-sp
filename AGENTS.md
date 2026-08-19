# AGENTS.md

> Universal guidance for AI coding assistants working in this repository.
> See also: [CLAUDE.md](./CLAUDE.md) for Claude-specific detailed instructions.

## Project Overview

**infrahub-demo-sp** is a comprehensive demonstration of multi-vendor MPLS network provisioning using [Infrahub](https://docs.infrahub.app). It showcases:

- Multi-vendor MPLS backbone topology (Arista EOS, Cisco IOS-XR, Juniper Junos, Nokia SR OS)
- L3VPN service provisioning via Streamlit Service Catalog
- Containerlab lab artifact generation for testing
- Configuration management with Jinja2 templates
- Validation checks for MPLS network devices
- Infrastructure-as-code patterns

## Quick Start

```bash
# Install dependencies
uv sync

# Start Infrahub containers
uv run invoke start

# Bootstrap schemas, menu, and data
uv run invoke bootstrap

# Run full initialization (destroy + start + bootstrap + demo)
uv run invoke init
```

## Build and Test Commands

```bash
# Run every test, unit and integration
uv run invoke test

# Run only the tests that need no Infrahub deployment (fast, no Docker)
uv run invoke test-unit

# End-to-end suite: boots a throwaway Infrahub. Needs Docker, takes tens of minutes.
uv run invoke test-integration              # core scope
uv run invoke test-integration --tier full  # includes the `extended` tier

# Lint and type check -- the same tasks CI runs
uv run invoke lint            # Full suite: markdown, YAML, ruff, mypy
uv run invoke lint-ruff       # ruff check + ruff format --check --diff
uv run invoke lint-mypy       # mypy --show-error-codes .
uv run invoke lint-yaml       # yamllint -s .
uv run invoke lint-markdown   # rumdl check .

# Apply formatting and safe lint fixes
uv run invoke format
```

A bare `uv run pytest` collects `tests/` in full, integration included, so it needs Docker.

## Code Style Guidelines

### Python

- **Type hints required** on all function signatures
- **Docstrings required** for all modules, classes, and functions (Google-style)
- Format with `ruff`, pass `mypy` type checking
- PascalCase for classes, snake_case for functions/variables
- Max line length: 100 characters
- Use `pathlib` over `os.path`

### Naming Conventions

- **Schema Nodes**: PascalCase (`LocationBuilding`, `DcimDevice`)
- **Attributes/Relationships**: snake_case (`device_type`, `parent_location`)
- **Namespaces**: PascalCase (`Dcim`, `Ipam`, `Service`, `Design`)

## Architecture Overview

This project follows Infrahub's SDK pattern with five core component types:

```text
schemas/      → Data models, relationships, constraints
generators/   → Create infrastructure topology programmatically
transforms/   → Convert Infrahub data to device configurations
checks/       → Validate configurations and connectivity
templates/    → Jinja2 templates for device configurations
```

### Data Flow

```text
Schema Definition → Data Loading → Generator Execution → Transform Processing → Configuration Generation
                                         ↓
                                   Validation Checks
```

### Key Files

- `.infrahub.yml` - Central registry for all components (transforms, generators, checks, queries)
- `tasks.py` - Invoke task definitions for automation
- `pyproject.toml` - Project dependencies and tool configuration

## Testing Instructions

1. **Before committing**: Run `uv run invoke test-unit` -- the same gate CI's `unit-test` job runs,
   and it needs no Docker
2. **For new features**: Add tests under `tests/unit/`
3. **Use mocks**: Mock external dependencies with `unittest.mock`
4. **Test both paths**: Cover success and failure scenarios
5. **Test tiers**: Tiers are defined by directory. `tests/unit/` needs no deployment;
   `tests/integration/` boots its own Infrahub stack with `infrahub-testcontainers` (Docker
   required, no `invoke start`) at the version declared in `[dependency-groups] dev`.

### Infrahub release automation

`.github/workflows/update-infrahub.yml` answers a `repository_dispatch` from
`opsmill/infrahub` or `infrahub-sdk-python`, bumps the specifier on an
`update-infrahub-<version>` branch and opens a PR, whose CI is the QA for that release. It
serves both pipelines -- there is no `update-infrahub-sdk.yml` -- and also runs by hand from
the Actions tab.

## Post-Change Validation

**IMPORTANT**: After making code changes, always run the full lint suite:

```bash
uv run invoke lint  # Runs: rumdl, yamllint, ruff, mypy
```

This ensures:

- Markdown passes rumdl at its default `any` severity
- YAML files are valid under `yamllint -s`, where warnings are errors
- Python code passes ruff linting and is correctly formatted
- Type hints are correct (mypy)

CI calls these same tasks, so passing locally means passing in CI -- except mypy, which checks
whatever interpreter you run it with; only CI's 3.11 leg verifies the `requires-python` floor.

## Security Considerations

- Never commit `.env` files or credentials
- API tokens in documentation are demo tokens for local development only
- Avoid introducing OWASP top 10 vulnerabilities (XSS, SQL injection, command injection)
- Validate external inputs at system boundaries

## PR and Commit Guidelines

- Use descriptive commit messages focusing on "why" not "what"
- Reference issue numbers where applicable
- Do not auto-commit - only commit when explicitly requested
- **Always run `uv run invoke lint` after code changes and before commits/PRs**

## Development Environment

- **Package Manager**: `uv` (required)
- **Python Version**: 3.11 through 3.14
- **Container Runtime**: Docker (for Infrahub)

### Environment Variables

Required in `.env`:

```bash
INFRAHUB_ADDRESS="http://localhost:8000"
INFRAHUB_API_TOKEN="<your-token>"
```

Optional:

```bash
INFRAHUB_GIT_LOCAL="true"  # Use local repo instead of GitHub
```

## Common Pitfalls

1. **Missing `uv sync`** - Always run after pulling changes
2. **Missing type hints** - All functions require complete annotations
3. **Jinja2 autoescape** - Set `autoescape=False` for device configs
4. **HTML entities** - Use `get_interface_roles()` which handles HTML decoding
5. **Missing `.infrahub.yml` entries** - Register all generators/transforms/checks
6. **Wrong box style in Rich** - Use `box.SIMPLE` for terminal compatibility

## Sub-Project Guidelines

- [docs/AGENTS.md](./docs/AGENTS.md) - Documentation site (Docusaurus)

## Resources

- [Infrahub Documentation](https://docs.infrahub.app)
- [Infrahub SDK Documentation](https://docs.infrahub.app/python-sdk/)
- [CLAUDE.md](./CLAUDE.md) - Detailed Claude Code instructions
