# Contributing

This document is intended for Grayhaven Systems LLC employees and assumes that
this repository has been initialized and configured appropriately.

If you are not a Grayhaven Systems LLC employee, we still welcome your support
and contribution.

## Table of Contents

- [Development Setup](#development-setup)
- [Workflow](#workflow)
- [Local Validation](#local-validation)
- [Pull Requests](#pull-requests)
- [Documentation Guidelines](#documentation-guidelines)

## Development Setup

Install Ansible runtime and validation dependencies:

```bash
sudo dnf install ShellCheck
python3 -m pip install --require-hashes --requirement pip3_requirements.txt
python3 -m pip install --user --requirement requirements-dev.txt
python3 -m pip install ansible-lint==26.6.0 yamllint
npm install --global markdownlint-cli2
scripts/install-galaxy-collections
```

Install Prometheus `promtool` to run the alert behavior unit tests.

The Python packages in `pip3_requirements.txt` are pinned with hashes and cover
the runtime prerequisites for the DigitalOcean Ansible collection used by the
dynamic inventory.
Ansible Galaxy collection artifacts are version-pinned in
`galaxy_requirements.yml` and checksum-verified through `galaxy_requirements.sha256`.

When updating runtime Python dependencies, keep the full dependency closure
pinned in `pip3_requirements.txt` and include hashes for every entry. Validate
the file with:

```bash
python3 -m pip install --dry-run --require-hashes --requirement pip3_requirements.txt
```

When updating Ansible Galaxy collections, update `galaxy_requirements.yml`,
then run the following to update `galaxy_requirements.sha256`:

```bash
tmpdir="$(mktemp -d /tmp/grayhaven-galaxy.XXXXXX)"
ansible-galaxy collection download \
  --requirements-file galaxy_requirements.yml
  --download-path "$tmpdir"
(cd "$tmpdir" && sha256sum *.tar.gz | sort -k2) > galaxy_requirements.sha256
rm -rf "$tmpdir"
```

Then validate the refreshed collection lock file with:

```bash
scripts/install-galaxy-collections
```

[Back to top](#contributing)

## Workflow

1. Create a GitHub issue.
2. Create a focused feature branch from the updated `main` branch for the issue.
3. Sign all commits and reference the issue number.
4. Validate changes locally.
5. Open a ready-for-review (not draft) pull request targeting `main`. Direct
   pushes to `main` are forbidden.
6. Resolve every review conversation before merging.
7. Merge only after the required checks `validate` and `Unit Tests` pass. Do
   not bypass required checks or branch protections.
8. Use squash merging only; merge commits and rebase merging are not allowed.
9. After the pull request is merged, manually delete the feature branch because
   automatic branch deletion is disabled.

[Back to top](#contributing)

## Local Validation

Validate formatting and syntax from the repository root:

```bash
git ls-files '*.yml' '*.yaml' | xargs -r yamllint
ansible-lint .
find playbooks -type f \( -name "*.yml" -o -name "*.yaml" \) -print0 \
  | xargs -0 -n1 ansible-playbook -i tests/fixtures/syntax-inventory.yml --connection=local --syntax-check
shellcheck files/grayhaven-ansible-runner files/grayhaven-ansible-poller roles/admin_access/files/gtmux
python3 -m ruff check files roles scripts tests
python3 -m py_compile \
  files/grayhaven-gcs-restic-bucket-cleanup \
  files/grayhaven-reboot-notify \
  files/grayhaven-refresh-motd \
  files/grayhaven-ansible-automation-status \
  files/grayhaven-redact-output \
  roles/deploy_websites/files/grayhaven-website-deploy \
  roles/restic_backup/files/grayhaven-restic-duration \
  roles/restic_backup/files/grayhaven-restic-workflow \
  roles/observability/files/grayhaven-grafana-alert-sync \
  roles/observability/files/grayhaven-irm-alert-groups-textfile \
  roles/observability/files/grayhaven-observability-textfile \
  roles/observability/files/grayhaven-gcs-restic-stale-buckets
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage report
python3 -m coverage xml
test -s shell-coverage.xml
git ls-files '*.md' | xargs -r markdownlint-cli2
```

Before committing changes, also check the current diff for whitespace errors:

```bash
git diff --check
```

When adding scripts, Python helpers, workflows, or managed templates, update
CI and local validation commands so the new files are checked.

[Back to top](#contributing)

## Pull Requests

Pull requests must meet all of these requirements to be merged:

- Target `main` with a ready-for-review (not draft) pull request; direct pushes
  to `main` are forbidden.
- Reference or close a GitHub issue as appropriate.
- Contain signed commits.
- Have no open review conversations.
- Pass exactly the required checks `validate` and `Unit Tests`.
- Use squash merging only; do not use merge commits or rebase merging, and do
  not bypass required checks or branch protections.
- Manually delete the feature branch after merging because automatic branch
  deletion is disabled.
- Document all changes appropriately.

[Back to top](#contributing)

## Documentation Guidelines

Keep user-facing behavior documented in `README.md` and architecture details in
[configuration architecture](docs/configuration-architecture.md). Add inline
comments for non-obvious implementation decisions, security boundaries, and
assumptions. Avoid comments that merely restate straightforward Ansible tasks
or shell commands.

[Back to top](#contributing)
