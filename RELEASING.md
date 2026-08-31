# Releasing Biomni Bridge

This is the repository-level release checklist. It assumes the one-time PyPI Trusted Publisher setup has already been completed.

## What the automation does

- `ci.yml`: tests normal pushes and pull requests; publishes nothing.
- `docker-check.yml`: builds Docker for validation; publishes nothing.
- `release.yml`: triggered by a `vX.Y.Z` tag and runs, in order:
  1. CI verification;
  2. wheel/source-package build and validation;
  3. native `linux/amd64` + `linux/arm64` build and push to GHCR;
  4. PyPI Trusted Publishing;
  5. GitHub Release creation.

If a stage fails, later publishing stages do not run.

## Before the first PyPI release

Create a GitHub environment named `pypi`. Then configure a PyPI pending Trusted Publisher:

```text
Project:      biomni-bridge
Owner:        YOUR_GITHUB_USERNAME
Repository:   biomni-bridge
Workflow:     release.yml
Environment:  pypi
```

No PyPI token belongs in GitHub Secrets.

## Release checklist

1. Choose the next version.
2. Change only `[project].version` in `pyproject.toml`.
3. Install release tools and validate locally:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev,release]'
make release-check
```

4. Optional but recommended Docker test:

```bash
docker build -t biomni-bridge:release-test .
```

5. Commit and push the version change:

```bash
git add pyproject.toml
# Include any other intentional release changes too.
git commit -m "Release 0.3.1"
git push origin main
```

6. Wait for the normal `CI` workflow to pass on `main`.
7. Tag the exact same commit and push the tag:

```bash
git tag v0.3.1
git push origin v0.3.1
```

8. Watch the single `Release` workflow in GitHub Actions. Do not create another tag while it is running.
9. When it is green, verify the outputs.

GHCR:

```bash
docker buildx imagetools inspect ghcr.io/anondo1969/biomni-bridge:0.3.1
docker pull ghcr.io/YOUR_USERNAME/biomni-bridge:0.3.1
```

PyPI, in a fresh Python 3.11 environment:

```bash
python3.11 -m venv /tmp/biomni-bridge-pypi-check
source /tmp/biomni-bridge-pypi-check/bin/activate
python -m pip install --upgrade pip
python -m pip install biomni-bridge==0.3.1
python -c 'import biomni_bridge; print(biomni_bridge.__version__)'
```

The release workflow also creates the GitHub Release after PyPI succeeds.

## If something fails

- **CI fails:** fix the code; do not tag.
- **Docker check fails:** inspect the failed architecture/build step, fix it, push normally, and wait for CI again.
- **Release fails before PyPI:** fix the cause. If PyPI has not received the version, you may delete the failed Git tag locally/remotely and recreate it only if you are certain no immutable release artifact was published. Safer practice is usually to bump to the next patch version.
- **PyPI already contains the version:** that version cannot be replaced. Increment the version and release again.
- **Trusted Publishing fails:** verify the PyPI publisher says repository `biomni-bridge`, workflow `release.yml`, environment `pypi`, and your exact GitHub owner. Then rerun the failed GitHub job.
- **GHCR pull is denied after a successful release:** open the package settings in GitHub and make the package Public if public pulls are intended.
