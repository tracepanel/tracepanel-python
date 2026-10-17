---
name: release
description: "Cut a release of the tracepanel package. Use when asked to release, publish, bump the version, or write release notes. Covers the version and the agent's pin, the order against tracepanel-agent, the tag that publishes, and the changelog."
---

# Releasing tracepanel

A release is a version bump, a changelog entry and a tag. Publishing is
automatic: `.github/workflows/release.yml` runs on a tag that names a version,
checks the tag against the version and the agent's pin, installs the newest
published `tracepanel-agent` the pin allows, runs the linters and the tests against it,
builds, and uploads to PyPI with `uv publish` through trusted publishing. No
token lives in the repository.

## A minor together, a patch on its own

`tracepanel` depends on any patch of `tracepanel-agent` at its own minor
version: `tracepanel-agent>=0.3.1,<0.4` for `tracepanel` 0.3.x.

- **A new minor** (`0.4.0`) is released with the agent's: release the agent
  from the tracepanel repository first, wait until it is on PyPI, then this
  one with the pin `>=0.4.0,<0.5`.
- **A patch** of this package (`0.3.2`) needs no agent release. Keep the pin,
  or raise its lower bound to the agent patch this release needs.
- **A patch of the agent** needs no release here: users get it with the next
  install or upgrade.

The release workflow checks that the pin covers the version's minor, and
checks out the agent's schemas at the tag of the agent it installed.

## The version, in three places

- `[project] version` in `pyproject.toml`
- the `tracepanel-agent>=` pin in its `dependencies`, on a new minor
- `__version__` in `src/tracepanel/__init__.py`

Versions are PEP 440: `0.1.0a1` for an alpha, `0.1.0` for a release. The tag is
the version, without a `v`.

```bash
uv version 0.1.0a1   # writes pyproject.toml and uv.lock
# set __version__ to the same version (and on a new minor, the agent's pin)
uv lock
```

## Choosing the number

The project is on `0.x`, so the minor number carries the weight. Something
breaks for a user who upgrades, such as a renamed setting or a changed default,
or this package and the agent need each other's new version: bump the minor,
with the agent. Nothing breaks: bump the patch, on its own.

## The release itself

```bash
uv run poe lint
uv run poe test
# write the CHANGELOG.md entry
git commit -a -m "Release 0.1.0a1"
git push
git tag 0.1.0a1 && git push origin 0.1.0a1
```

The run waits in the `pypi` environment for a reviewer to approve the upload.

`django-tracepanel` pins `tracepanel[django]` to the same minor: release it
after this one on a new minor. A patch here needs no release of it.

## The changelog

`CHANGELOG.md` holds the history, newest version first. A version has the date
it was released and the headings it needs, `Added`, `Changed`, `Fixed`,
`Removed`, in that order, none of them empty. Write it for someone deciding
whether to upgrade, in the voice of the `docs` skill. Leave out what nobody
outside the repository can see.

## Once, on the publishing side

Trusted publishing needs a publisher on PyPI (owner `tracepanel`, repository
`tracepanel-python`, workflow `release.yml`, environment `pypi`) and the `pypi`
environment on GitHub.
While the tracepanel repository is private, the `TRACEPANEL_AGENT_TOKEN` secret
gives the workflow read access to it.
