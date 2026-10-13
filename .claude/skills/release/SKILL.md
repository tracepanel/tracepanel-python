---
name: release
description: "Cut a release of the tracepanel package. Use when asked to release, publish, bump the version, or write release notes. Covers the version and the agent's pin, the order against tracepanel-agent, the tag that publishes, and the changelog."
---

# Releasing tracepanel

A release is a version bump, a changelog entry and a tag. Publishing is
automatic: `.github/workflows/release.yml` runs on a tag that names a version,
checks the tag against the version and the agent's pin, installs the published
`tracepanel-agent` of that version, runs the linters and the tests against it,
builds, and uploads to PyPI with `uv publish` through trusted publishing. No
token lives in the repository.

## The agent goes first

`tracepanel` depends on `tracepanel-agent` at exactly its own version. Release
the agent from the tracepanel repository first, with the same version, and wait
until it is on PyPI. The release workflow checks out the tracepanel repository
at the tag of the same name, so tag both the same way.

## The version, in three places

- `[project] version` in `pyproject.toml`
- the `tracepanel-agent==` pin in its `dependencies`
- `__version__` in `src/tracepanel/__init__.py`

Versions are PEP 440: `0.1.0a1` for an alpha, `0.1.0` for a release. The tag is
the version, without a `v`.

```bash
uv version 0.1.0a1   # writes pyproject.toml and uv.lock
# set the tracepanel-agent pin and __version__ to the same version
uv lock
```

## Choosing the number

The project is on `0.x`, so the minor number carries the weight. Something
breaks for a user who upgrades, such as a renamed setting or a changed default:
bump the minor. Nothing breaks: bump the patch.

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

`django-tracepanel` pins `tracepanel[django]` at the same version: release it
after this one.

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
