# Grayhaven Systems LLC Configuration (Ansible)

[![CI](https://github.com/grayhavensystemsllc/grayhaven-config-ansible/actions/workflows/ci.yml/badge.svg)](https://github.com/grayhavensystemsllc/grayhaven-config-ansible/actions/workflows/ci.yml)
[![Unit Tests](https://github.com/grayhavensystemsllc/grayhaven-config-ansible/actions/workflows/unit-tests.yml/badge.svg)](https://github.com/grayhavensystemsllc/grayhaven-config-ansible/actions/workflows/unit-tests.yml)
[![Codecov](https://codecov.io/gh/grayhavensystemsllc/grayhaven-config-ansible/graph/badge.svg)](https://codecov.io/gh/grayhavensystemsllc/grayhaven-config-ansible)

Ansible configuration management for Grayhaven Systems LLC infrastructure.

This repository is public for transparency and operational demonstration. It
shows how Grayhaven Systems LLC manages its own infrastructure, but it does not
store client infrastructure, client credentials, private deployment data,
private SSH keys, secrets, generated state, or other private operational data.

OpenTofu provisions the servers, cloud-init runs the bootstrap playbook on first
boot, and the active control bastion runs the full Ansible playbook on a timer,
on repository-change poller events, or by manual invocation.

## Table of Contents

- [Scope](#scope)
- [Manual Runner Invocation](#manual-runner-invocation)
- [Maintenance Playbooks](#maintenance-playbooks)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Scope

- Create and secure the `ansible` automation account.
- Bootstrap only the minimum access needed for ongoing convergence.
- Pull encrypted operational values from the private
  `grayhaven-vault` repository
  on the active control bastion.
- Run full Ansible convergence from the active control bastion.
- Manage configured users, SSH access, sudo access, and absent-user homedir
  archival.
- Enforce a shared managed-host baseline for access, SSH, SELinux, time sync,
  package state, DigitalOcean agents, firewalld, local backups, and local host
  aliases.
- Send operational Discord notifications for convergence events and server
  reboots.
- Optionally publish production metrics, selected logs, and managed alert rules
  to Grafana Cloud.
  Managed alerts have a minimum five-minute pending period to tolerate brief
  failures; longer rule-specific periods are preserved.
- Deploy repository-backed static websites for hosted domains, or render the
  generic fallback site when no repository is configured.
- Manage the Time Tracker WebAuthn relying-party ID and exact HTTPS origin from
  the configured Time Tracker hostname.
- Manage Nginx, host TLS certificates, load-balancer backend behavior, dev
  basic authentication, per-domain htpasswd files, and certificate renewal on
  web hosts.
- Manage the private MCP container, persistent state, HTTPS certificate, and
  verified encrypted database backups on dedicated MCP hosts. Environment-specific
  public and encrypted configuration is downloaded from the private repository’s
  `main` branch using the existing registry token with `repo` and `read:packages`
  scopes. Both files use the same commit and are validated before installation.
  A minimal container init process forwards signals and reaps exited children.
  Remote maintenance uses a bounded automation-user CPU quota and weight on MCP
  hosts, configured in Vault `operations.yml`.
- Validate Ansible, YAML, Markdown, and shell scripts through GitHub Actions.

This repository is not a general-purpose deployment template. Deploying similar
automation for another organization requires review and adaptation.

[Back to top](#grayhaven-systems-llc-configuration-ansible)

## Manual Runner Invocation

Connect to the active control bastion through the easy `bastion.*` DNS record,
then run:

```bash
sudo systemctl start grayhaven-ansible-runner.service
```

Useful status commands:

```bash
sudo systemctl status grayhaven-ansible-runner.service
sudo systemctl status grayhaven-ansible-runner.timer
sudo systemctl status grayhaven-ansible-poller.timer
sudo journalctl -u grayhaven-ansible-runner.service
```

See [manual runner invocation](docs/operations.md#manual-runner-invocation)
and [runner and poller status](docs/operations.md#runner-and-poller-status)
for operational details.

[Back to top](#grayhaven-systems-llc-configuration-ansible)

## Maintenance Playbooks

Maintenance playbooks are manual change-control tools intended to be run from
the active control bastion by an authorized administrator.

Supported maintenance playbooks rotate the persisted Ansible Vault password,
the vault deployment SSH keypair, the Ansible control key used for managed-host
SSH, and clean up stale remote restic buckets. The operations guide also
documents backup and restoration operations. See
[vault password rotation](docs/operations.md#vault-password-rotation),
[deploy key rotation](docs/operations.md#deploy-key-rotation),
[Ansible control key rotation](docs/operations.md#ansible-control-key-rotation),
[backup & restoration operations](docs/operations.md#backup--restoration-operations),
and [GCS restic bucket cleanup](docs/operations.md#gcs-restic-bucket-cleanup)
for prerequisites, commands, and validation steps.

[Back to top](#grayhaven-systems-llc-configuration-ansible)

## Documentation

- [Configuration Architecture](docs/configuration-architecture.md)
- [Observability Architecture](docs/observability-architecture.md)
- [Operator Tmux Architecture](docs/operator-tmux-architecture.md)
- [Operations](docs/operations.md)
- [Validation & Safety Checks](docs/validation-safety.md)

[Back to top](#grayhaven-systems-llc-configuration-ansible)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, validation
commands, and contribution guidelines.

[Back to top](#grayhaven-systems-llc-configuration-ansible)

## License

[MIT](LICENSE)

[Back to top](#grayhaven-systems-llc-configuration-ansible)
