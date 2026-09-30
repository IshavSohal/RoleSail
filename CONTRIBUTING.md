# Contributing to RoleSail

Thank you for your interest in contributing to RoleSail. This guide covers everything you need to get started.

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Node.js 20.19 or higher (dashboard contributors and release builds only)
- Git

### Clone and Install

```bash
git clone https://github.com/IshavSohal/RoleSail.git
cd RoleSail
pip install -e ".[dev]"
playwright install chromium
```

This installs RoleSail in editable mode with all development dependencies (pytest, ruff, etc.) and downloads the Chromium browser binary for Playwright.

### Dashboard Development

The dashboard is a React + TypeScript application in `frontend/`. Start the
Python API in one terminal:

```bash
rolesail dashboard --no-open
```

Then start Vite in another terminal. Its development server proxies `/api` to
the Python server on port 8765.

```bash
cd frontend
npm install
npm run dev
```

Use `npm test`, `npm run typecheck`, `npm run lint`, and `npm run build` to test
and build the frontend. Production assets are written to
`src/rolesail/web_dist/` and are committed so installed Python packages do
not require Node.js. Before a release, run `npm run build:check` and commit any
changed assets.

### Verify Installation

```bash
rolesail --version
pytest tests/ -v
ruff check src/
```

## How to Contribute

### Adding New Workday Employers

Workday employer portals are configured in `config/employers.yaml`. To add a new employer:

1. Find the company's Workday career portal URL (usually `https://company.wd5.myworkdaysite.com/`)
2. Identify the Workday instance number (wd1, wd3, wd5, etc.) and the tenant ID
3. Add an entry to `config/employers.yaml`:

```yaml
- name: "Company Name"
  tenant: "company_tenant_id"
  instance: "wd5"
  url: "https://company.wd5.myworkdaysite.com/en-US/recruiting"
```

4. Test discovery: `rolesail discover --employer "Company Name"`
5. Submit a PR with the new entry

### Adding Ashby or Lever Employers

Ashby and Lever expose public, unauthenticated job-board APIs. Add an Ashby
company to `src/rolesail/config/ashby_companies.yaml` using the slug from
`https://jobs.ashbyhq.com/<slug>`:

```yaml
companies:
  example: { name: "Example", board: "example" }
```

Add a Lever company to `src/rolesail/config/lever_companies.yaml` using the
slug from `https://jobs.lever.co/<slug>`. Lever boards hosted in the EU must
also set `region: eu`:

```yaml
companies:
  example: { name: "Example", site: "example" }
  example_eu: { name: "Example EU", site: "example-eu", region: "eu" }
```

Run `rolesail run discover` and confirm the source reports jobs without an
adapter error.

### Adding New Career Sites

Direct career site scrapers are configured in `config/sites.yaml`. To add a new site:

1. Inspect the company's careers page and identify the job listing structure
2. Add an entry to `config/sites.yaml` with CSS selectors:

```yaml
- name: "Company Name"
  url: "https://company.com/careers"
  selectors:
    job_list: ".job-listing"
    title: ".job-title"
    location: ".job-location"
    link: "a.job-link"
    description: ".job-description"
```

3. Test: `rolesail discover --site "Company Name"`
4. Submit a PR

### Bug Fixes and Features

1. Check existing [issues](https://github.com/IshavSohal/RoleSail/issues) to avoid duplicating work
2. For new features, open an issue first to discuss the approach
3. Fork the repo and create a feature branch from `main`
4. Write your code with type hints and docstrings
5. Add tests for new functionality
6. Update the CHANGELOG.md under an `[Unreleased]` section
7. Submit a PR

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_scoring.py -v

# Run with coverage
pytest tests/ --cov=src/rolesail --cov-report=term-missing
```

## Linting and Code Style

RoleSail uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Check for issues
ruff check src/

# Auto-fix what can be fixed
ruff check src/ --fix

# Format code
ruff format src/
```

### Code Style Guidelines

- **Type hints**: All function signatures must have type annotations
- **Docstrings**: All public functions and classes must have docstrings (Google style)
- **Naming**: snake_case for functions and variables, PascalCase for classes
- **Imports**: Sorted by Ruff (isort-compatible)
- **Line length**: 100 characters maximum

## PR Guidelines

- **One feature per PR.** Keep changes focused and reviewable.
- **Include tests.** New features need test coverage. Bug fixes need a regression test.
- **Update CHANGELOG.md.** Add your changes under `[Unreleased]`.
- **Write a clear PR description.** Explain what changed and why.
- **Keep commits clean.** Squash fixup commits before requesting review.
- **CI must pass.** All linting and tests must be green.

## Project Structure

```
RoleSail/
├── src/rolesail/       # Main package
│   ├── __init__.py
│   ├── cli.py            # CLI entry points
│   ├── discover/         # Stage 1: job discovery scrapers
│   ├── enrich/           # Stage 2: description extraction
│   ├── score/            # Stage 3: AI scoring
│   ├── tailor/           # Stage 4: resume tailoring
│   ├── cover/            # Stage 5: cover letter generation
│   ├── apply/            # Stage 6: browser automation
│   └── utils/            # Shared utilities
├── config/               # Default configuration files
├── tests/                # Test suite
├── docs/                 # Documentation
└── pyproject.toml        # Package configuration
```

## License

By contributing to RoleSail, you agree that your contributions will be licensed under the [GNU Affero General Public License v3.0](LICENSE).
