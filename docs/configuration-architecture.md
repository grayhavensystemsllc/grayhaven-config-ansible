# Configuration Architecture

Grayhaven Systems LLC configuration is split into first-boot bootstrap and ongoing
full-playbook convergence.

## Table of Contents

- [Bootstrap Phase](#bootstrap-phase)
- [Runner And Poller](#runner-and-poller)
- [Vault Loading](#vault-loading)
- [Managed Host Baseline](#managed-host-baseline)
- [Managed Users](#managed-users)
- [Operator Tmux Console](#operator-tmux-console)
- [Web Hosting](#web-hosting)
- [Time Tracker](#time-tracker)
- [Firewalld Policy](#firewalld-policy)
- [Backups](#backups)
- [Observability](#observability)
- [Unit Testing](#unit-testing)
- [Access Model](#access-model)

## Bootstrap Phase

The
[grayhaven-infra-opentofu](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu)
repository renders
[role-specific cloud-init user-data](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/runtime-architecture.md#configuration-handoff)
for each droplet. During first boot, cloud-init installs Ansible, writes
bootstrap variables to `/etc/grayhaven/bootstrap/bootstrap-vars.yml`, and runs
`ansible-pull` against this repository.

The bootstrap playbook prepares the host for management:

- validates required handoff variables early;
- sets the system hostname;
- creates swap space to provide memory headroom during package and automation
  bursts;
- creates the password-locked `ansible` automation user;
- grants the `ansible` user passwordless sudo;
- installs the bootstrap and current control public keys for first-boot
  `ansible` access;
- installs the vault deployment key and temporary first-boot automation key on
  bastion hosts;
- stores the Ansible Vault password only on bastion hosts;
- installs runner and poller services on bastion hosts;
- enables runner and poller timers only on the declared control bastion.

The bootstrap variables file is removed before the initial runner starts. It is
only a first-boot handoff file and is not a long-term source of truth for
managed secrets.

Bootstrap does not persist application secrets, human user secrets, Discord
webhooks, or DigitalOcean service tokens outside the vault model.

[Back to top](#configuration-architecture)

## Runner And Poller

The active control bastion is the Ansible control node. The runner keeps
persistent checkouts of:

- `/home/ansible/grayhaven-config-ansible`
- `/home/ansible/grayhaven-vault`

The runner and poller services execute as the `ansible` automation user. The
runner installs hash-pinned Python runtime dependencies and checksum-verified,
version-pinned Ansible Galaxy collections under the ansible-owned
`/home/ansible/ansible-runner` workspace. Ansible commands from that virtual
environment are linked into `/usr/local/bin` for operator use without changing
ownership of the virtual environment. The runner decrypts vault values through
Ansible Vault, prepares SSH host keys for newly discovered hosts, and runs
`playbooks/site.yml`. Existing managed host-key trust is preserved. The playbook
refreshes controller hostname mappings before connecting to managed hosts.

`grayhaven-vault` SSH access uses the vault deployment SSH key stored at
`/home/ansible/.ssh/grayhaven_vault_deploy_key` on bastion hosts. Repository SSH
access uses a dedicated GitHub known-hosts file refreshed from GitHub-published
host keys instead of accepting host keys dynamically at runtime.

Before each playbook run, the runner refreshes live DigitalOcean inventory data.
The current control-node status and TLS mode are derived from environment
droplet tags so OpenTofu policy changes can be applied without relying on stale
first-boot bootstrap values.

The poller checks the public configuration repository and `grayhaven-vault` for
changes every five minutes. It compares each configured remote ref with HEAD in
the runner's corresponding local checkout (`CHECKOUT_DIR` and
`VAULT_CHECKOUT_DIR`). If either differs or a checkout is missing, it starts the
normal runner service. Manual convergences therefore update the comparison
without a separate poller cache. Existing `config.ref` and `vault.ref` files are
unused. The daily runner timer remains a convergence safety net.

The poller uses a five-minute calendar timer so its next execution does not
rely on retained service activation history. Convergence reloads unit files,
restarts changed timers, repairs unarmed timers, and verifies enabled schedules
using systemd's integer clock values. An elapsed timer without a future
execution fails verification. A timer whose oneshot is currently executing is
valid; verification never restarts the runner service from inside convergence.
`grayhaven-ansible-automation-status` provides the same read-only health result
to the MCP SSH integration.

The poller retries transient GitHub metadata lookup failures before failing the
poller service. Persistent lookup failures remain visible in systemd and the
poller journal.

Only the active control bastion runs the scheduled runner and poller. Other
bastions are configured as SSH jump points and are managed over SSH by the
active control bastion.

[Back to top](#configuration-architecture)

## Vault Loading

`grayhaven-vault` supplies runtime selectors and encrypted operational values.
Its structure follows the
[file schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md)
documented in the
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository. Plaintext files are grouped by purpose: `policy/` contains
infrastructure and firewall policy, `grafana/` contains observability settings,
alert definitions and UID mappings, and the root contains `config.yml`,
`server_configuration.yml` and `ansible.yml`. Each setting has one source.
Fixed implementation values live in the config repository; server environment,
certificate settings and application identity are derived from their canonical
inputs. Files under `vault/` are decrypted at runtime.

Application roles register their containers with the observability role during
each convergence. Host process monitoring and generated alert rules use those
registrations; container names are not configured separately in Vault.

Production reads the vault `main` branch. Staging reads the vault `staging`
branch.

Vault values provide:

- root password hash;
- managed users;
- restic password;
- DigitalOcean inventory and DNS API tokens;
- Discord notification webhooks;
- Ansible control key values;
- hosted domain definitions and development basic-auth htpasswd entries.

[Back to top](#configuration-architecture)

## Managed Host Baseline

Repository definitions are prepared before general package upgrades. The
operating system release package is updated first, followed by refreshed
distribution facts and EPEL definitions. On AlmaLinux 10, EPEL metadata targets
the matching operating system minor release, including after a stable minor
upgrade. DigitalOcean and Grafana repositories are also configured during this
phase.

The full playbook enforces a common managed-host baseline before applying
role-specific configuration. The baseline is implemented as focused roles so
inventory metadata, package policy, automation-user state, backups, firewall
policy, intrusion prevention, runner services, and host status are owned by
smaller task boundaries. The baseline covers:

- root password hash and root SSH authorized-key removal;
- `ansible` automation user, sudo policy, and SSH key state;
- SSH daemon hardening for public-key-only access, login timing, authentication
  attempt limits, and idle-session keepalives;
- fail2ban SSH intrusion-prevention jails;
- managed SSH known-host entries on bastion;
- SELinux enforcing mode;
- persistent systemd journal storage;
- common administration packages and unnecessary service removal;
- the DigitalOcean metrics agent from the managed DigitalOcean package
  repository;
- managed swap;
- configured DNS resolvers through NetworkManager;
- AlmaLinux time synchronization;
- firewalld inbound rules from the vault firewall policy;
- local host aliases such as `example-app-prod-web-01` and
  `example-app-prod-web-01.internal`;
- local Ansible facts at `/etc/ansible/facts.d/grayhaven.fact`;
- root-only Discord webhook configuration for post-reboot notifications;
- local encrypted restic backups.

The playbook records whether each host requires a reboot and displays only that
host's status in the login MOTD. Separate completion notifications list hosts
with and without a required reboot using the warning and success colors
respectively. Empty host lists do not produce a notification. A managed
`grayhaven-refresh-motd.service` refreshes the MOTD at boot so a completed
reboot clears the login warning before the next convergence run. Ansible does
not reboot servers automatically.

Full convergence also installs `grayhaven-reboot-notify.service` on managed
hosts. The service sends one informational `Server Rebooted` Discord
notification after each boot and records the boot ID locally so the same boot is
not reported more than once. The Discord webhook values come from the encrypted
vault and are persisted only in the root-readable local configuration used by
the service.

Managed hosts publish a local Ansible fact at
`/etc/ansible/facts.d/grayhaven.fact`. This exposes the host role,
environment, control-node status, TLS mode, DigitalOcean tags, and known IPv4
addresses to local scripts and administrators without requiring direct
DigitalOcean API access.

[Back to top](#configuration-architecture)

## Managed Users

Managed users are defined in `vault/common.yml`. The
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository documents the expected
[user file schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#vaultcommonyml)
and
[user management procedure](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/operations.md#managing-users).
Users may be marked `present` or `absent`.

`present` users are created with configured password hashes, SSH keys, and
optional password sudo access. `absent` users are removed. If `home_mode` is
omitted for an absent user, the home directory is archived and compressed as a
`.tar.gz` file before deletion. `home_mode: delete` deletes the home directory
without archiving it.

Homedir archives are not encrypted by the archive process. They are included in
the encrypted local restic backup set by default.

Present managed users with `sudo: true` are covered by sudo journal logging and
interactive root-shell command logging. One-shot `sudo` commands remain visible
through normal sudo logs. Interactive root Bash shells emit concise
`grayhaven-root-command` journal entries with the command text and origin user
when known. Command output is not captured.

[Back to top](#configuration-architecture)

## Operator Tmux Console

Sudo-capable managed users on bastions receive the standard Grayhaven Systems
LLC tmux configuration, theme, and `gtmux` launcher. `gtmux` attaches to the
standard `Grayhaven Systems LLC` tmux session, creating it first when needed.

Per-user tmux workspaces are optional and are stored in `grayhaven-vault` under
`files/tmux-workspaces/`. If auto-attach is enabled for a user in
`grayhaven-vault`, logging in to that bastion user over interactive SSH runs
`gtmux` automatically. The
[operator tmux architecture](operator-tmux-architecture.md)
documentation describes workspace behavior and capture guidance. See the
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
[managed user schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#managed-user-entries)
for the public example shape.

All present managed users receive a stable forwarded-agent socket helper at
`~/.ssh/ssh_auth_sock` so SSH agent forwarding remains usable inside tmux and
inside user-managed shell workflows.

[Back to top](#configuration-architecture)

## Web Hosting

Web hosts install Nginx and serve vault-defined hosted domains. A hosted domain
can define `deployment.type: static` with a public HTTPS Git repository. Static
deployments copy `site/frontend/` from the repository `main` branch to the
apex and `www` document root, and `site/frontend/` from the `dev` branch to the
development document root. Domains without repository configuration are rendered
from the generic fallback templates in the website deployment role.

Development output retains the `[Dev]` page-title cue and displays a small,
fixed status dot with the accessible name `Development Site`. Repository-backed
sites receive the indicator through isolated Shadow DOM, so neither the hosted
site nor the indicator can restyle the other. The generic Development fallback
uses the same visual contract from its managed stylesheet. Production output
and the generic production fallback contain no Development indicator.

Repository-backed domains also receive a small deployment webhook at
`/.grayhaven/deploy` on the apex hostname. The endpoint accepts signed GitHub
Actions deployment requests, verifies the repository webhook secret from
`grayhaven-vault`, and deploys only configured repositories and the `main` or
`dev` branches. In host TLS mode, Nginx also restricts the endpoint to GitHub
source ranges. Deployments are locked per domain and branch so independent sites
or branches can deploy without blocking each other.

Convergence refreshes the GitHub source ranges only from a successful, nonempty
metadata response. If GitHub's metadata endpoint is temporarily unavailable,
the existing known-good Nginx allowlist remains installed and convergence
continues.

When load-balancer TLS is active with two or more web hosts, the web host that
receives the public webhook coordinates private web-to-web fanout over short
hostnames that resolve to private addresses. Peer fanout requests are signed with
the environment-wide `web_deploy_fanout_secret` from `grayhaven-vault`, deploy
only locally, and do not recursively fan out. Fanout is disabled for
single-web-host environments. Public webhook deployments and peer fanout
deployments target the exact commit SHA from the GitHub webhook payload.

Each local deployment writes status metadata under
`/var/lib/grayhaven/website-deploy/status/` for observability support.

Host TLS mode issues certificates with Let's Encrypt through DNS-01 validation
using the role-specific DigitalOcean DNS token from the vault. Certbot renewals
are handled by the system timer installed by the Certbot package. A deploy hook
reloads Nginx after certificate renewal.

In host TLS mode, Nginx rejects hostnames that are not explicitly configured so
unknown hostnames do not fall through to a managed web site.

Ansible records which certificate environment last issued each host TLS
certificate. If `certificate.environment` changes in `grayhaven-vault`,
Ansible reissues the certificate for the new environment. This should be
changed deliberately and infrequently because production certificate issuance
is rate limited by the certificate authority.

Load balancer TLS mode configures web hosts as HTTP backends. Certbot renewal
is disabled, local host certificate material is removed, and TLS is terminated
by the DigitalOcean load balancer managed by OpenTofu.

Development hostnames require HTTP basic authentication. Each hosted domain
defines its own `dev.htpasswd_entries` list in `vault/web.yml`, and Ansible
writes a separate htpasswd file for each domain. If `dev.auth_realm` is
omitted, the realm defaults to `<domain> Development Environment`.

Website configuration runs before Fail2ban so each enabled authentication jail
has its log file before the service starts.

In host TLS mode, web hosts also enable a fail2ban `nginx-http-auth` jail for
development basic-auth failures. The jail is disabled in load-balancer TLS mode
until client IP handling is explicitly configured for that path.

New hosted domains require both
[environment DNS policy](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/policy.md#environment-dns-policy)
in
[grayhaven-infra-opentofu](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu)
and matching `hosted_domains` data in `grayhaven-vault`. See the
[DNS architecture documentation](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/dns-architecture.md)
in the `grayhaven-infra-opentofu` repository and the
[hosted domain DNS coordination documentation](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#hosted-domain-dns-coordination)
in the
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository.

[Back to top](#configuration-architecture)

## Time Tracker

Time Tracker is a required web-host service. The public `timetracker` mapping
in `grayhaven-vault/config.yml` selects its hostname, immutable application
image digest, persistent directories, and optional verified-backup retention.
The separate encrypted `timetracker_secrets` mapping in `vault/web.yml`
supplies the Flask signing key, SQLCipher passphrase, and initial users.

On every convergence, the branding role updates its control-node checkout from
the public `grayhaven-branding` repository's `main` branch and verifies that
the checkout matches the fetched remote revision. The Time Tracker role copies
only the required wordmark, favicon, and font assets into the application
branding directory. Neither Ansible nor the application image embeds private
branding credentials.

Web hosts run the application as a rootful system Quadlet so systemd and Podman
own the container lifecycle. The application process itself runs as dedicated
UID and GID 777. Convergence fails if either identifier belongs to another
account. The container uses an immutable `@sha256:` image reference, a
read-only root filesystem, loopback-only port publication, dropped
capabilities, no-new-privileges enforcement, a PID limit, and explicit health
checks. Persistent data, verified backups, branding, and secret files use
separate SELinux-labeled bind mounts. A minimal container init process forwards
signals and reaps exited children.

Nginx is the only public application entry point. It terminates host TLS or
accepts traffic from the managed load balancer, forwards a controlled HTTPS
proxy context, and does not expose the container health endpoint publicly. The
role persistently enables the SELinux `httpd_can_network_connect` boolean so
Nginx can reach the loopback-only container upstream while SELinux remains
enforcing. Time Tracker access logging removes query strings and redacts
shared-report tokens before log files can be collected.

Bootstrap users are reconciled only when the database has no users. Initial
passwords must be changed at first sign-in. Once users exist, account management
belongs to the application rather than repeated Ansible convergence.

The role derives the non-secret `WEBAUTHN_RP_ID` and `WEBAUTHN_ORIGIN` container
environment values from the validated public `timetracker.hostname` mapping.
The RP ID is the hostname only, without a scheme, port, or path; the origin is
exactly `https://` followed by that hostname. Hostname validation rejects
malformed values before the template is rendered. These values are managed
configuration, not entries in `timetracker_secrets` or files under the secret
directory. The application's exact-origin, fail-closed behavior remains in
force, and the values are never derived from request headers.

[Back to top](#configuration-architecture)

## Firewalld Policy

The firewalld policy role reads the environment firewall policy from `firewall.yml`
in the checked-out `grayhaven-vault` repository and caches it locally under
`/etc/grayhaven/firewall/policy.yml`. The
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository documents the
[firewall policy schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#firewallyml).

If the vault firewall policy is unavailable and a cached policy exists, Ansible
uses the cached policy and sends an informational Discord notification. If no
valid policy is available, Ansible sends a warning notification and skips
firewalld policy changes, preserving the current local firewall state while
allowing the rest of the playbook to converge.

At this time, Ansible enforces host inbound firewalld policy from the
environment-specific vault firewall policy file. DigitalOcean cloud firewalls
continue to enforce provider-side inbound and outbound policy.

When web hosts run behind load-balancer TLS, OpenTofu restricts cloud firewall
origin HTTP access to the DigitalOcean load balancer and removes direct web
origin HTTPS access. Ansible applies the matching host-side behavior by keeping
only the local web origin ports needed for backend service.
For load-balanced environments with two or more web hosts, Ansible also opens
TCP `8791` between web hosts on private addresses for website deployment fanout.

For SSH from bastion to managed hosts, local firewalld allows the environment
VPC CIDR from `grayhaven-vault` `network.vpc_cidr` as well as known bastion
private addresses. This prevents active control-node failover from locking the
new control bastion out of existing managed hosts. DigitalOcean cloud firewalls
still enforce the tighter bastion source-tag boundary before traffic reaches
the host.

[Back to top](#configuration-architecture)

## Backups

Each managed server creates encrypted restic backups using settings from
`grayhaven-vault/config.yml`. The
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository documents the
[backup settings schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#configyml).
The local repository path defaults to
`/var/backups/restic`, and the homedir archive path defaults to
`/var/backups/deleted-homedir-archives`.

Ansible installs the Backupctl operator utility from the exact immutable commit
in the three-field `grayhaven_backupctl` mapping: `repo_url`, `checkout_dir`,
and `version`. The version must be a lowercase full commit SHA. Convergence
checks out and verifies that commit, then compares the installed
`grayhaven-backupctl --version` identity with the checkout's canonical
`VERSION` plus the commit's seven-character abbreviation. Missing or different
identities use the same installation path, supporting both upgrades and
intentional downgrades; an unchanged identity skips runtime dependency and
implementation installation.

By default, backups include:

- configured homedir archive path;
- `/home`;
- `/var/log`.

On web hosts, the backup service first asks the running Time Tracker container
to create an encrypted online backup in its dedicated backup directory and
verifies that artifact before publishing its final filename. Restic includes
that verified-artifact directory and explicitly excludes the live SQLCipher
database plus its WAL and SHM sidecars. Local artifacts are pruned only after
the selected restic repositories complete successfully. On MCP hosts, the
backup service creates and verifies an encrypted online database snapshot in
its backup directory before backing up the selected repositories. Bastion
backups skip the application snapshot step. The scheduled backup and all
Backupctl repository selections use the same managed workflow.

When `backup.include` is set in `grayhaven-vault/config.yml`, that explicit
list is appended after the configured homedir archive path. This keeps
removed-user archives in the encrypted local backup set even when an environment
uses a custom include list.

`backup.exclude` is passed to restic as an exclude file and can exclude paths
that were included automatically. Excluding the configured homedir archive path
deliberately opts removed-user archives out of backup.

Retention groups snapshots by host. Changing the configured include paths does
not create a separate daily-retention stream for that host.

The local restic repository is encrypted. Local backups are not a substitute for
disaster recovery.

Remote GCS repositories are optional. When enabled, Ansible creates one GCS
bucket per managed host before restic is initialized, labels the bucket with
Grayhaven Systems LLC resource metadata, disables bucket versioning and soft
delete, and points restic at `gs:<short-hostname>-restic:/`.

The remote repository uses the same restic encryption password as the local
repository. Remote backup credentials come from `grayhaven-vault` and are
stored on managed hosts only in the root-readable backup configuration needed
by restic.

Ansible installs daily backup and weekly integrity-check timers. The integrity
check validates the local restic repository and, when configured, the remote
GCS repository.

Remote backups can be enabled or disabled during normal convergence. Enabling
remote backups creates any missing buckets and updates restic configuration.
Disabling remote backups removes the remote repository from the managed restic
configuration but leaves existing buckets in place. The cleanup playbook is
required to delete stale remote buckets; when remote backups are disabled for
the environment, all labeled Ansible-managed restic buckets for that
environment are considered stale.

Operational backup procedures should include regular restore testing. The
destructive cleanup procedure for stale remote buckets is documented in
[Operations](operations.md#gcs-restic-bucket-cleanup).

[Back to top](#configuration-architecture)

## Observability

Production hosts can optionally publish metrics, selected logs, and managed
Grafana Cloud alert rules. Observability is tag-driven and configured through
`grayhaven-vault` values so Ansible can combine live inventory, host role data,
and runtime service state during convergence.

Grafana Cloud integration is supported only for the `prod` environment. If it
is enabled elsewhere, convergence fails before installing or syncing
observability components.

The [observability architecture](observability-architecture.md) documentation
describes the metrics, logs, alert ownership boundary, and production-only
behavior.

[Back to top](#configuration-architecture)

## Unit Testing

The unit suite exercises the repository's managed Python programs and the
significant decision logic in its shell programs. Python coverage is measured
with branch coverage. Shell line coverage uses an isolated Bash `DEBUG` trap
for `gtmux` and the Ansible Galaxy collection installer. Runner and poller
tests source those programs behind a test-only execution guard and exercise
their argument handling, command dispatch, and repository-change decisions.

The unit-testing boundary has these limitations:

- It does not run integration tests. External APIs, subprocesses, filesystem
  failures, and remote responses are represented with temporary paths and
  controlled test doubles.
- It tests Python and shell programs, not Ansible playbooks, roles, modules, or
  convergence behavior. Ansible syntax checks, linting,
  staging convergence, and production convergence are separate validation
  layers.
- It does not prove live systemd, Podman, SELinux, Nginx, restic, Grafana,
  DigitalOcean, Google Cloud, GitHub, or network behavior.
- It does not simulate an entire managed host merely to execute runner and
  poller integration paths. Those paths are validated through real
  convergence and operational verification.
- Direct command-entry wrappers and their final process-exit handling may not
  be included in coverage when the underlying `main()` behavior is already
  exercised directly.
- Timing, concurrency, service-restart ordering, and failure recovery across
  multiple hosts remain integration and operational concerns.

[Back to top](#configuration-architecture)

## Access Model

Human administrative access and automation access are intentionally separate.

Human administrators use personal accounts and local SSH agent forwarding.
Personal private keys are not stored on Grayhaven Systems LLC servers.

The `ansible` account is automation-only. OpenTofu supplies bootstrap
deployment key material for first-boot automation and `grayhaven-vault`
repository access. On bastions, that key is retained separately as the vault
deployment SSH key used for `grayhaven-vault` repository access.

Full convergence replaces the temporary first-boot `id_ed25519` automation key
with the vault-defined Ansible control key and enforces the matching public key
as the only managed `ansible` authorized key. This supports scheduled
convergence from the active control bastion without depending on a human SSH
agent.

[Back to top](#configuration-architecture)
