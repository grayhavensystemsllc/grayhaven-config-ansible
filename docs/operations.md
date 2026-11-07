# Operations

Grayhaven Systems LLC configuration is normally applied by the active control
bastion. This document covers manual runner use and maintenance playbooks.

## Table of Contents

- [Manual Runner Invocation](#manual-runner-invocation)
- [Runner And Poller Status](#runner-and-poller-status)
- [Switching Deployed Configuration Branches](#switching-deployed-configuration-branches)
- [Manual Discord Notification Test](#manual-discord-notification-test)
- [Ansible Discord Webhook Rotation](#ansible-discord-webhook-rotation)
- [DigitalOcean Inventory Token Rotation](#digitalocean-inventory-token-rotation)
- [Certbot DigitalOcean DNS Token Rotation](#certbot-digitalocean-dns-token-rotation)
- [Grafana Alloy Token Rotation](#grafana-alloy-token-rotation)
- [Grafana Alert Rule API Token Rotation](#grafana-alert-rule-api-token-rotation)
- [Grafana IRM Alert-Group Token Rotation](#grafana-irm-alert-group-token-rotation)
- [GCS Restic Service-Account Key Rotation](#gcs-restic-service-account-key-rotation)
- [Hosted-Domain Deployment Webhook Secret Rotation](#hosted-domain-deployment-webhook-secret-rotation)
- [Web Deployment Fanout Secret Rotation](#web-deployment-fanout-secret-rotation)
- [Vault Password Rotation](#vault-password-rotation)
- [Deploy Key Rotation](#deploy-key-rotation)
- [Ansible Control Key Rotation](#ansible-control-key-rotation)
- [Backup & Restoration Operations](#backup--restoration-operations)
- [Time Tracker Database Restore](#time-tracker-database-restore)
- [Checking Time Tracker Health](#checking-time-tracker-health)
- [Changing the Managed Timezone](#changing-the-managed-timezone)
- [Website Repository Deployments](#website-repository-deployments)
- [GCS Restic Bucket Cleanup](#gcs-restic-bucket-cleanup)
- [Root Command Audit Trail](#root-command-audit-trail)
- [Fail2ban Administration](#fail2ban-administration)
- [Operator Tmux Console](#operator-tmux-console)
- [Grafana Cloud Observability](#grafana-cloud-observability)
- [Infrastructure Policy Changes](#infrastructure-policy-changes)

## Manual Runner Invocation

Connect to the active control bastion through the easy `bastion.*` DNS record,
then start the runner:

```bash
sudo systemctl start grayhaven-ansible-runner.service
```

The runner refreshes the public configuration checkout and `grayhaven-vault`
checkout, loads vault values, refreshes live DigitalOcean inventory, prepares
SSH known hosts, and runs `playbooks/site.yml`.

Manual commands that use the DigitalOcean inventory require
`GRAYHAVEN_ENVIRONMENT` to be set. If it is unset, inventory selection fails
closed instead of defaulting to an environment.

Maintenance playbooks are also launched through
`/usr/local/sbin/grayhaven-ansible-runner` so they use the same checkout,
vault, runtime, lock, and dynamic inventory path as normal convergence.

[Back to top](#operations)

## Runner And Poller Status

Useful service and log commands:

```bash
sudo systemctl status grayhaven-ansible-runner.service
sudo systemctl status grayhaven-ansible-runner.timer
sudo systemctl status grayhaven-ansible-poller.service
sudo systemctl status grayhaven-ansible-poller.timer
sudo systemctl status grayhaven-reboot-notify.service
sudo journalctl -u grayhaven-ansible-runner.service
sudo journalctl -u grayhaven-ansible-poller.service
sudo journalctl -u grayhaven-reboot-notify.service
```

The poller checks the tracked configuration and vault refs every five minutes.
When it detects a change, it starts the runner and then records the observed
refs. If the runner trigger fails, the ref state is not advanced so the next
poll can retry the same change. Pushing to the deployed configuration branch or
deployed vault branch can therefore start convergence automatically within the
next poll interval.

The poller also retries transient GitHub metadata lookup failures before
failing the poller service. Persistent lookup failures still surface as service
failures and should be investigated through the poller journal.

Discord configuration notifications retry transient transport errors, HTTP 429
rate limits, and HTTP 5xx responses up to three times. Rate-limited requests
honor Discord's `Retry-After` value. If those retries are exhausted, Ansible
logs a sanitized warning and allows an otherwise successful convergence to
complete; rejected notification requests still fail convergence.
Retries stop instead of waiting more than five minutes for a single response.

The poller is not a convergence queue. If another repository change is pushed
while convergence is already running, that change may not be applied by the
active run. In that case, run manual convergence or wait for the next scheduled
runner pass.

Schedule changes that trigger convergence outside the daily Restic backup
window. Check `grayhaven-restic-backup.timer` on affected hosts before pushing;
its scheduled start is 02:37 local time with up to a 15-minute randomized
delay:

```bash
sudo systemctl list-timers grayhaven-restic-backup.timer
```

If convergence and a backup overlap, inspect the backup service and journal
after convergence finishes:

```bash
sudo systemctl show grayhaven-restic-backup.service \
  -p Result -p ExecMainStatus --no-pager
sudo journalctl -u grayhaven-restic-backup.service -n 40 --no-pager
```

Ansible temporary files may disappear during a backup, leaving a saved local
snapshot but an unsuccessful job and an unchanged last-success metric. If the
service failed, start it again after convergence and check the result:

```bash
sudo systemctl start grayhaven-restic-backup.service
sudo systemctl show grayhaven-restic-backup.service \
  -p Result -p ExecMainStatus --no-pager
```

Confirm `Result=success` and `ExecMainStatus=0` before treating the backup as
complete.

Managed hosts send one informational `Server Rebooted` Discord notification
after each boot. The local `grayhaven-reboot-notify.service` records the current
boot ID after a successful notification so repeated service checks do not resend
the same reboot event.

[Back to top](#operations)

## Switching Deployed Configuration Branches

An environment can be switched between deployed configuration branches without
rebuilding infrastructure. This is mainly useful for staging branch testing, but
the procedure is environment agnostic. The target branch must already exist in
the public configuration repository.

Connect to the active control bastion and update the runner environment:

```bash
TARGET_BRANCH="<target-branch>"

sudo sed -i "s|^REPO_REF=.*|REPO_REF='${TARGET_BRANCH}'|" \
  /etc/grayhaven/ansible/runner.env
```

Start the poller to compare local checkout HEADs with the configured remote refs
and trigger convergence when they differ:

```bash
sudo systemctl start grayhaven-ansible-poller.service
sudo journalctl -u grayhaven-ansible-poller.service -n 80 --no-pager
sudo journalctl -u grayhaven-ansible-runner.service -f --no-pager
```

After convergence succeeds, verify the active checkout. The poller compares
this HEAD directly with the remote ref; no cached poller state needs updating:

```bash
sudo grep '^REPO_REF=' /etc/grayhaven/ansible/runner.env
sudo -iu ansible \
  git -C /home/ansible/grayhaven-config-ansible rev-parse --abbrev-ref HEAD
sudo -iu ansible \
  git -C /home/ansible/grayhaven-config-ansible rev-parse --short HEAD
```

Do not delete the previously deployed branch until the environment has
successfully converged from the new branch.

[Back to top](#operations)

## Manual Discord Notification Test

Use a test webhook when manually checking Discord notification formatting. The
manual request should include an explicit user agent because Discord may reject
default client user agents.

```bash
WEBHOOK_URL="<insert webhook URL here>"

curl \
  --silent \
  --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --user-agent "ExampleClient/1.0 (+https://example.com)" \
  --data @- \
  "$WEBHOOK_URL" <<'JSON'
{
  "embeds": [
    {
      "title": "Server Rebooted",
      "description": "manual-test.example.com\n\nEnvironment: test\n2026-06-12 01:00 PM CDT",
      "color": 5811424
    }
  ]
}
JSON
```

Discord returns HTTP `204` when the webhook accepts the notification.

[Back to top](#operations)

## Ansible Discord Webhook Rotation

Rotate the webhook from the workstation's `grayhaven-vault` checkout. Use
`main` and `discord_webhooks.production` for production, or `staging` and
`discord_webhooks.staging` for staging. Keep the old webhook active until the
replacement is in use.

1. Create a replacement webhook in Discord. Leave the old webhook in place.

2. Select the target environment branch and pull the latest changes:

   ```bash
   git checkout <branch>
   git pull
   ```

3. Decrypt and edit the file using that environment's vault password:

   ```bash
   ansible-vault decrypt vault/bastion.yml
   vim vault/bastion.yml
   ```

   Replace the selected `discord_webhooks` URL, preserving the YAML structure.

4. Re-encrypt, commit, and push the file:

   ```bash
   ansible-vault encrypt vault/bastion.yml
   git add vault/bastion.yml
   git commit -S -m "Rotate Ansible Discord webhook (Refs #<issue>)"
   git push
   ```

5. For an active environment, allow the poller to start convergence or start
   the runner manually. Confirm a successful play recap and that the
   replacement webhook receives the configuration notifications.

6. Remove the old webhook after the replacement is confirmed. If the
   environment is not running, the vault change is ready for its next
   deployment; notification delivery cannot yet be verified.

[Back to top](#operations)

## DigitalOcean Inventory Token Rotation

Rotate the token from the workstation's `grayhaven-vault` checkout. Repeat the
procedure separately for each target environment: `staging` for staging and
`main` for production. Keep the environment branches independent.

1. Select the target environment branch and pull the latest changes. Replace
   `<branch>` with `staging` or `main`:

   ```bash
   git checkout <branch>
   git pull
   ```

2. Decrypt the file using the target environment's vault password:

   ```bash
   ansible-vault decrypt vault/bastion.yml
   ```

3. Open the file in Vim:

   ```bash
   vim vault/bastion.yml
   ```

   Find `digitalocean_inventory_api_token` and replace its value with the new
   token, preserving the YAML key and formatting.
   Use a token with the
   [documented inventory permissions](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/operations.md#generating-api-keys).

4. Re-encrypt the file with the
   same environment's vault password:

   ```bash
   ansible-vault encrypt vault/bastion.yml
   ```

5. Stage the encrypted file and commit the change. Replace `<message>` with a
   concise description of the rotation:

   ```bash
   git add vault/bastion.yml
   git commit -S -m "<message>"
   ```

6. Push the updated branch:

   ```bash
   git push
   ```

7. Connect to the target environment's active control bastion and monitor
   convergence. The enabled poller normally detects the update within five
   minutes:

   ```bash
   sudo systemctl status grayhaven-ansible-runner.service
   sudo journalctl -u grayhaven-ansible-runner.service -f
   ```

   Verify a new run starts after the push and completes successfully. If it
   does not start automatically, start it manually when ready:

   ```bash
   sudo systemctl start grayhaven-ansible-runner.service
   ```

Follow [Runner And Poller Status](#runner-and-poller-status) to verify the run
completes successfully. The runner updates its local vault checkout before
loading dynamic inventory, so this procedure also works when the previous token
has expired. Bastions only read the published vault; they do not modify it.
If the poller is intentionally disabled for maintenance, leave it disabled and
start convergence manually when ready.

[Back to top](#operations)

## Certbot DigitalOcean DNS Token Rotation

Rotate the token from the workstation's `grayhaven-vault` checkout. Repeat the
procedure separately for each target environment: `staging` for staging and
`main` for production. Keep the environment branches independent.

1. Select the target environment branch and pull the latest changes. Replace
   `<branch>` with `staging` or `main`:

   ```bash
   git checkout <branch>
   git pull
   ```

2. Decrypt the file using the target environment's vault password:

   ```bash
   ansible-vault decrypt vault/web.yml
   ```

3. Open the file in Vim:

   ```bash
   vim vault/web.yml
   ```

   Find `digitalocean_dns_api_token` and replace its value with the new token,
   preserving the YAML key and formatting. Use a
   token with the [documented DNS permissions](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/operations.md#generating-api-keys).

4. Re-encrypt the file with the same environment's vault password:

   ```bash
   ansible-vault encrypt vault/web.yml
   ```

5. Stage the encrypted file and commit the change. Replace `<message>` with a
   concise description of the rotation:

   ```bash
   git add vault/web.yml
   git commit -S -m "<message>"
   ```

6. Push the updated branch:

   ```bash
   git push
   ```

7. Connect to the target environment's active control bastion and monitor
   convergence. The enabled poller normally detects the update within five
   minutes:

   ```bash
   sudo systemctl status grayhaven-ansible-runner.service
   sudo journalctl -u grayhaven-ansible-runner.service -f
   ```

   Verify a new run starts after the push and completes successfully. If it
   does not start automatically, start it manually when ready:

   ```bash
   sudo systemctl start grayhaven-ansible-runner.service
   ```

Convergence deploys the updated DNS credential to the web hosts. On each target
web host, verify Certbot can complete a DNS-01 challenge with a renewal dry run:

```bash
sudo certbot renew --dry-run
```

Confirm the dry run succeeds for the managed certificates. Successful
convergence alone does not verify renewal if no certificate needed renewing.
If the poller is intentionally disabled for maintenance, leave it disabled and
start convergence manually when ready.

[Back to top](#operations)

## Grafana Alloy Token Rotation

Rotate the collector token from the workstation's `grayhaven-vault` checkout
on `main`. Grafana Cloud integration is production-only. Create the replacement
using the [collector token setup](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/grafana-cloud-setup.md#collector-token).
Keep the old token valid until the replacement is verified when possible.

1. Before revoking the old token or deploying its replacement, create a
   time-limited Grafana Cloud silence for the following telemetry rules. Use
   equality matchers `configured_by=ansible`, `client=grayhaven`, and
   `environment=prod`, plus a regular-expression matcher on `check`:

   ```text
   ^(metrics_data|gcs_service_telemetry_success|gcs_stale_bucket_check_stale|gcs_operation_telemetry_stale|google_monitoring_service_telemetry_success|google_monitoring_telemetry_stale|proton_telemetry_success)$
   ```

2. Select the production branch and pull the latest changes:

   ```bash
   git checkout main
   git pull
   ```

3. Decrypt the file using the production vault password:

   ```bash
   ansible-vault decrypt vault/common.yml
   ```

4. Open the file in Vim:

   ```bash
   vim vault/common.yml
   ```

   Replace `grafana_cloud.alloy_api_key` with the new token, preserving the
   YAML key and formatting.

5. Re-encrypt the file with the production vault password:

   ```bash
   ansible-vault encrypt vault/common.yml
   ```

6. Stage the encrypted file and commit the change. Replace `<message>` with a
   concise description of the rotation:

   ```bash
   git add vault/common.yml
   git commit -S -m "<message>"
   ```

7. Push the updated branch:

   ```bash
   git push
   ```

8. Connect to the active control bastion and monitor convergence. The enabled
   poller normally detects the update within five minutes:

   ```bash
   sudo systemctl status grayhaven-ansible-runner.service
   sudo journalctl -u grayhaven-ansible-runner.service -f
   ```

   Verify a new run starts after the push and completes successfully. If it
   does not start automatically, start it manually when ready:

   ```bash
   sudo systemctl start grayhaven-ansible-runner.service
   ```

   If the poller is intentionally disabled for maintenance, leave it disabled
   and start convergence manually when ready. Follow
   [Runner And Poller Status](#runner-and-poller-status) to verify completion.

9. Confirm the Alloy restart, readiness, and remote-write checks succeed on
   both hosts. In Grafana, verify fresh metrics from both hosts and fresh logs
   from hosts configured for log shipping. Confirm the eight telemetry rules
   have returned to normal. Revoke the old collector token if it remains valid,
   then confirm telemetry continues arriving with the replacement.

10. Remove the rotation silence to restore notifications immediately. Verify
    that it is no longer active and the telemetry rules remain normal. Any
    existing rotation-related alert groups should resolve automatically;
    verify their resolution rather than manually resolving them to hide a
    remaining telemetry failure.

[Back to top](#operations)

## Grafana Alert Rule API Token Rotation

Rotate this token from the workstation's `grayhaven-vault` checkout on `main`.
Create the replacement using the [alert rule API token setup](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/grafana-cloud-setup.md#alert-rule-api-token).
Keep the old token valid until the replacement is verified when possible.

1. Select the production branch and pull the latest changes:

   ```bash
   git checkout main
   git pull
   ```

2. Decrypt the file using the production vault password:

   ```bash
   ansible-vault decrypt vault/common.yml
   ```

3. Open the file in Vim:

   ```bash
   vim vault/common.yml
   ```

   Replace `grafana_cloud.alerting.api_token` with the new token, preserving
   the YAML key and formatting.

4. Re-encrypt the file with the production vault password:

   ```bash
   ansible-vault encrypt vault/common.yml
   ```

5. Stage the encrypted file and commit the change. Replace `<message>` with a
   concise description of the rotation:

   ```bash
   git add vault/common.yml
   git commit -S -m "<message>"
   ```

6. Push the updated branch:

   ```bash
   git push
   ```

7. Connect to the active control bastion and monitor convergence. The enabled
   poller normally detects the update within five minutes:

   ```bash
   sudo systemctl status grayhaven-ansible-runner.service
   sudo journalctl -u grayhaven-ansible-runner.service -f
   ```

   Verify a new run starts after the push and completes successfully. If it
   does not start automatically, start it manually when ready:

   ```bash
   sudo systemctl start grayhaven-ansible-runner.service
   ```

   If the poller is intentionally disabled for maintenance, leave it disabled
   and start convergence manually when ready. Follow
   [Runner And Poller Status](#runner-and-poller-status) to verify completion.

8. Confirm `Sync Grafana Cloud managed alert rules` succeeds on the active
   control bastion. An unchanged (`ok`) result is successful synchronization;
   the task is intentionally skipped on other hosts. Verify the managed rules
   remain healthy in Grafana, then revoke the old token if it remains valid.

[Back to top](#operations)

## Grafana IRM Alert-Group Token Rotation

Rotate this token from the workstation's `grayhaven-vault` checkout on `main`.
Keep the old token active until the replacement is verified.

1. Create a replacement token for the existing Grafana Cloud service account
   using the
   [documented IRM alert-group token permissions](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/grafana-cloud-setup.md#irm-alert-groups-api-token).

2. Select the production branch and pull the latest changes:

   ```bash
   git checkout main
   git pull
   ```

3. Decrypt and edit the file using the production vault password:

   ```bash
   ansible-vault decrypt vault/common.yml
   vim vault/common.yml
   ```

   Replace `grafana_cloud.irm_alert_groups.api_token`.

4. Re-encrypt, commit, and push the file:

   ```bash
   ansible-vault encrypt vault/common.yml
   git add vault/common.yml
   git commit -S -m "Rotate Grafana IRM alert-group token (Refs #<issue>)"
   git push
   ```

5. Monitor production convergence and require a successful play recap. The
   runner installs the replacement token in the control bastion's protected
   IRM reporting configuration.

6. Confirm the collector succeeds with the replacement token:

   ```bash
   sudo systemctl start grayhaven-irm-alert-groups-textfile.service
   sudo systemctl show grayhaven-irm-alert-groups-textfile.service \
     -p Result -p ExecMainStatus -p ActiveState --no-pager
   ```

   `Result=success` and `ExecMainStatus=0` confirm a successful run. The
   one-shot service is normally inactive afterward.

7. Revoke the old token after the collector succeeds.

[Back to top](#operations)

## GCS Restic Service-Account Key Rotation

Rotate the key from the workstation's `grayhaven-vault` checkout. Keep the old
key active until remote access succeeds with the replacement.

1. Create a new JSON key for the existing GCS Restic service account. Retain
   the old key during the change.

2. Select the branch for the environment using GCS remote backups and pull the
   latest changes:

   ```bash
   git checkout <branch>
   git pull
   ```

3. Decrypt and edit the file using that environment's vault password:

   ```bash
   ansible-vault decrypt vault/common.yml
   vim vault/common.yml
   ```

   Replace `restic.remotes.gcs.credentials_json` with the complete replacement
   JSON, preserving the YAML block indentation.

4. Re-encrypt, commit, and push the file:

   ```bash
   ansible-vault encrypt vault/common.yml
   git add vault/common.yml
   git commit -S -m "Rotate GCS Restic service-account key (Refs #<issue>)"
   git push
   ```

5. Monitor convergence and require a successful play recap. The runner uses
   the replacement key to manage the GCS buckets and installs it for remote
   backups on each managed host. Follow the
   [backup timing and verification guidance](#runner-and-poller-status) if
   convergence overlaps a scheduled backup.

6. On each host using remote backups, confirm that the remote repository is
   accessible:

   ```bash
   sudo grayhaven-backupctl list --repo remote
   ```

7. Delete the old service-account key after the replacement works on each
   affected host.

[Back to top](#operations)

## Hosted-Domain Deployment Webhook Secret Rotation

Follow the
[hosted-domain webhook secret rotation procedure](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/operations.md#rotating-website-deployment-secrets)
to replace each domain's vault value and converge the web hosts. Use a distinct
secret for each hosted-domain repository.

After convergence succeeds, update `GRAYHAVEN_DEPLOY_WEBHOOK_SECRET` in each
hosted-domain repository on GitHub under **Security and quality → Secrets and
variables → Actions → Repository secrets**. Keep website deployments idle
between convergence and the matching GitHub secret updates. Run a normal
deployment for each domain to confirm the new secrets work.

[Back to top](#operations)

## Web Deployment Fanout Secret Rotation

Rotate this shared secret from the workstation's `grayhaven-vault` checkout.
Keep website deployments idle while the replacement is being deployed to all
web hosts.

1. Generate a new secret:

   ```bash
   openssl rand -hex 48
   ```

2. Select the target environment branch and pull the latest changes:

   ```bash
   git checkout <branch>
   git pull
   ```

3. Decrypt and edit the file using that environment's vault password:

   ```bash
   ansible-vault decrypt vault/web.yml
   vim vault/web.yml
   ```

   Replace `web_deploy_fanout_secret`.

4. Re-encrypt, commit, and push the file:

   ```bash
   ansible-vault encrypt vault/web.yml
   git add vault/web.yml
   git commit -S -m "Rotate web deployment fanout secret (Refs #<issue>)"
   git push
   ```

5. Monitor convergence and require a successful play recap on all web hosts.
   Resume website deployments after every web host has the replacement. When
   fanout is active, verify a deployment reaches each peer.

[Back to top](#operations)

## Vault Password Rotation

Vault password rotation has three coordinated parts:

1. Rekey the encrypted files in `grayhaven-vault` for the target
   environment branch by following the
   [vault password rotation documentation](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/operations.md#vault-password-rotation)
   in the
   [`grayhaven-vault-example`](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
   repository.
2. Update the matching variable in
   [`grayhaven-infra-opentofu`](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu),
   `TF_VAR_grayhaven_vault_password_staging` or
   `TF_VAR_grayhaven_vault_password_prod`, by following the
   [Ansible vault passphrase rotation documentation](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/operations.md#ansible-vault-passphrase-rotation)
   in the `grayhaven-infra-opentofu` repository so future droplets bootstrap
   with the new password.
3. Rotate the persisted password already stored on deployed bastions.

After the vault files and `grayhaven-infra-opentofu` environment variables are
updated, rotate the persisted vault password by placing the new value in a
temporary vars file that is readable by the `ansible` group and passing that
file to the runner:

```bash
umask 077
TEMP_VAULT_PASSWORD_FILE="$(mktemp /tmp/grayhaven-vault-password.XXXXXX.yml)"
printf '%s\n' 'new_vault_password: "<new password>"' > "$TEMP_VAULT_PASSWORD_FILE"
sudo chown root:ansible "$TEMP_VAULT_PASSWORD_FILE"
sudo chmod 0640 "$TEMP_VAULT_PASSWORD_FILE"
```

Run the rotation through the runner:

```bash
sudo /usr/local/sbin/grayhaven-ansible-runner \
  --rotate-vault-password \
  --extra-vars-file "$TEMP_VAULT_PASSWORD_FILE"
```

Avoid passing the new password directly on the shell command line. The runner
uses the temporary vars file to decrypt the updated `grayhaven-vault` checkout,
then the maintenance playbook updates the locally persisted vault password.
Remove the temporary vars file after the playbook completes and verify a normal
runner invocation can decrypt the vault with the new password:

```bash
sudo rm -f "$TEMP_VAULT_PASSWORD_FILE"
sudo systemctl start grayhaven-ansible-runner.service
sudo journalctl -u grayhaven-ansible-runner.service
```

[Back to top](#operations)

## Deploy Key Rotation

Rotate the vault deployment SSH keypair with
`playbooks/rotate-vault-deploy-key.yml`. OpenTofu supplies this keypair during
first boot so bastions can read `grayhaven-vault`.
After full convergence, bastions keep it only for `grayhaven-vault` repository
access at `/home/ansible/.ssh/grayhaven_vault_deploy_key`.

The deployment SSH keypair is shared by workspace environments. Log in through
the environment bastion DNS name so the session lands on the active control
bastion, then generate the staged keypair:

```bash
sudo -iu ansible ssh-keygen -f /home/ansible/new_ansible_deploy_key
```

Run the rotation:

```bash
sudo /usr/local/sbin/grayhaven-ansible-runner --rotate-vault-deploy-key
```

After the playbook completes, verify the runner can refresh the public
configuration repository and `grayhaven-vault`, then run a full
convergence pass. This playbook does not change the Ansible control key used
for managed-host SSH. The playbook removes the staged key files from bastions.

[Back to top](#operations)

## Ansible Control Key Rotation

Normal convergence enforces the vault-defined Ansible control key. After
updating `ansible_control.public_key` and `ansible_control.private_key` in
encrypted `grayhaven-vault` data, run the normal runner service from the active
control bastion.

The maintenance playbook remains available when you need to rotate the control
key directly from encrypted vault values. The runner refreshes the public
configuration and `grayhaven-vault` checkouts, loads live inventory, validates
the configured private and public key material, and then runs the maintenance
playbook:

```bash
sudo /usr/local/sbin/grayhaven-ansible-runner --rotate-ansible-control-key
```

After rotation, verify the active control bastion can SSH to managed hosts as
the `ansible` user and run a full convergence pass.

[Back to top](#operations)

## Backup & Restoration Operations

Managed servers install
[`grayhaven-backupctl`](https://github.com/grayhavensystemsllc/grayhaven-backupctl)
as the operator interface for manual backup creation, snapshot review, path
search, and local or remote restore work.

Use the `grayhaven-backupctl` operations guide for:

- [listing backups](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#listing-backups);
- [creating backups](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#creating-backups);
- [listing backup contents](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#listing-backup-contents);
- [finding backups containing paths](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#finding-backups-containing-paths);
- [restoring to a target directory](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#restoring-to-a-target-directory);
- [restoring in place](https://github.com/grayhavensystemsllc/grayhaven-backupctl/blob/main/docs/operations.md#restoring-in-place).

Restic installation and configuration, installation of the
`grayhaven-backupctl` utility, its isolated Python runtime, and its bash
completion script, as well as remote bucket management, are managed through
Ansible by this repository.

The private environment configuration selects Backupctl with exactly
`grayhaven_backupctl.repo_url`, `grayhaven_backupctl.checkout_dir`, and
`grayhaven_backupctl.version`. The version is a lowercase full commit SHA, not a
moving branch or tag. Convergence verifies the checkout at that exact commit
and requires the installed command to report
`grayhaven-backupctl MAJOR.MINOR.BUILD-SHORTSHA`. Runtime dependencies and the
implementation are reinstalled only when the installed identity is missing or
different; ordinary paths, wrapper, completion, permissions, and final identity
remain declaratively verified on every run.

[Back to top](#operations)

## Time Tracker Database Restore

The Time Tracker database requires an application-generated encrypted backup,
the matching SQLCipher passphrase, and the compatible immutable application
image. A restore must preserve the current database generation for rollback,
remove stale SQLite sidecars, restore managed ownership and SELinux context,
and use systemd to control the container.

Prefer a suitable verified artifact already present in the Time Tracker backup
directory. Follow the authoritative
[local Time Tracker database restore procedure](https://github.com/grayhavensystemsllc/grayhaven-timetracker/blob/main/docs/operations.md#restore-a-local-backup)
when one is available.

When no suitable local artifact is available, follow the authoritative
[Time Tracker database restore from restic procedure](https://github.com/grayhavensystemsllc/grayhaven-timetracker/blob/main/docs/operations.md#restore-a-backup-from-restic).
`grayhaven-backupctl` prefers the local restic repository and then uses the
remote restic repository when necessary.

If the SQLCipher passphrase must also change, complete the authoritative
[Time Tracker SQLCipher passphrase rotation procedure](https://github.com/grayhavensystemsllc/grayhaven-timetracker/blob/main/docs/operations.md#rotate-the-sqlcipher-passphrase)
before updating the encrypted configuration value. Ansible refuses to replace
the deployed passphrase while an existing database and deployed key are
present.

[Back to top](#operations)

## Checking Time Tracker Health

Use the authoritative
[Time Tracker service health procedure](https://github.com/grayhavensystemsllc/grayhaven-timetracker/blob/main/docs/operations.md#check-service-health)
to verify the systemd service, running immutable image digest, and application
health endpoint after convergence or maintenance.

[Back to top](#operations)

## Changing the Managed Timezone

The `timezone` Ansible variable controls both the operating-system
timezone and the Time Tracker container's `TZ` setting. Do not change the
timezone directly on a managed host or inside the container.

1. From the `grayhaven-vault` repository root on the operator workstation,
   open the operational settings file.

   ```bash
   "${EDITOR:-vi}" server_configuration.yml
   ```

2. Set `timezone` to the required IANA timezone name.

   ```yaml
   timezone: <Area/Location>
   ```

3. Confirm that the selected timezone exists in the workstation's timezone
   database.

   ```bash
   TIMEZONE="<Area/Location>"
   test -f "/usr/share/zoneinfo/${TIMEZONE}"
   ```

4. Validate the changed YAML and Git diff.

   ```bash
   yamllint server_configuration.yml
   git diff --check
   ```

5. Follow the Vault repository contribution workflow to publish the change
   on the branch tracked by the target environment.

6. From the active control bastion, start normal configuration convergence.

   ```bash
   sudo systemctl start grayhaven-ansible-runner.service
   ```

7. Review the convergence log and require a successful play recap.

   ```bash
   sudo tail -n 100 /var/log/grayhaven/ansible-runner/playbook.log
   ```

8. On a managed web host, verify the operating-system timezone and the Time
   Tracker container timezone.

   ```bash
   timedatectl show --property=Timezone --value
   sudo podman exec grayhaven-timetracker date '+%Z %z'
   ```

9. Follow the application
   [service health procedure](https://github.com/grayhavensystemsllc/grayhaven-timetracker/blob/main/docs/operations.md#check-service-health),
   then verify a representative time entry and report in the application.

[Back to top](#operations)

## Website Repository Deployments

Repository-backed hosted domains deploy `site/frontend/` from the `main` and
`dev` branches during normal convergence. The `main` branch is copied to the
apex document root, and the `dev` branch is copied to the protected development
document root.

The deployment webhook at `/.grayhaven/deploy` is installed only for
repository-backed domains. GitHub Actions calls this endpoint after validation
passes. In host TLS mode, Nginx allows only GitHub source ranges to reach the
endpoint. The local deployment service accepts only configured repository URLs,
the `main` or `dev` branch, and a valid HMAC signature made with the domain's
configured webhook secret.

The GitHub source-range refresh is best-effort. A temporary GitHub metadata
failure preserves the currently installed allowlist and does not fail the
configuration convergence.

In load-balancer TLS environments with two or more web hosts, whichever web host
receives the public webhook deploys the exact Git commit locally and then fans
the same deployment out to peer web hosts over private short hostnames. Peer
requests are authenticated with `web_deploy_fanout_secret` from
`grayhaven-vault`. If one or more peers fail, the coordinator attempts every
configured peer, logs the failed hostnames, and returns a failed webhook response
with the failed peer list.

Webhook deployment status is recorded in the
`grayhaven-website-deploy-webhook.service` journal:

```bash
sudo journalctl -u grayhaven-website-deploy-webhook.service --no-pager
```

[Back to top](#operations)

## GCS Restic Bucket Cleanup

Remote backup cleanup compares live DigitalOcean inventory with
Ansible-managed GCS restic buckets, then reports buckets whose host is no
longer present in inventory.

Run the dry run from the active control bastion:

```bash
sudo /usr/local/sbin/grayhaven-ansible-runner --cleanup-gcs-restic-buckets
```

Review the dry-run output carefully. Only buckets labeled as Ansible-managed
restic buckets for the configured client and environment are eligible for
deletion.

If the dry run lists only buckets that should be deleted, rerun with explicit
deletion enabled:

```bash
sudo /usr/local/sbin/grayhaven-ansible-runner \
  --cleanup-gcs-restic-buckets \
  --execute
```

This is destructive. Deleted remote backup buckets are not recoverable through
Grayhaven automation.

[Back to top](#operations)

## Root Command Audit Trail

Managed sudo-capable users are covered by sudo journal logging and interactive
root-shell command logging. This records one-shot `sudo` commands and commands
typed in interactive root Bash shells reached through `sudo su -` or `su -`.
Command output is not captured. Use the Loki or journal timestamp as the event
timestamp.

The primary review path is Grafana Cloud Loki when log shipping is enabled.
Useful starting queries:

```logql
{job="systemd_journal"} |= "COMMAND=" != " ansible :"
{job="systemd_journal"} |= "grayhaven-root-command"
```

For a local fallback on a managed host, run:

```bash
sudo journalctl --since today -t sudo -t grayhaven-root-command --no-pager \
  | grep -E 'COMMAND=|grayhaven-root-command' \
  | grep -v ' ansible :'
```

Avoid placing secrets directly on command lines. If a secret is accidentally
exposed through command arguments, rotate the affected secret immediately.

[Back to top](#operations)

## Fail2ban Administration

List configured jails and inspect a specific jail:

```bash
sudo fail2ban-client status
sudo fail2ban-client status sshd
sudo fail2ban-client status nginx-http-auth-example-com
```

Unban or manually ban an IP address:

```bash
sudo fail2ban-client set <jail> unbanip <ip-address>
sudo fail2ban-client set <jail> banip <ip-address>
```

Web development basic-auth jails are named from the hosted domain by replacing
characters outside `A-Z`, `a-z`, `0-9`, `_`, and `-` with `-`. For example,
`example.com` becomes `nginx-http-auth-example-com`.

[Back to top](#operations)

## Operator Tmux Console

On bastions, sudo-capable managed users receive the shared tmux configuration
and `gtmux` launcher. Run this command to attach to the standard operator
session, creating it first if needed:

```bash
gtmux
```

To reset and recreate the session from the configured workspace file, run this
from a normal shell outside tmux:

```bash
gtmux --reset
```

If tmux auto-attach is enabled for a user, logging in to that bastion user over
interactive SSH runs `gtmux` automatically. If that causes login trouble,
bypass it for one SSH session by setting the bypass variable in the remote
login shell:

```bash
ssh -A -tt <user>@bastion.example.com 'cat /etc/motd; GRAYHAVEN_TMUX_AUTO_ATTACH_BYPASS=1 exec bash -l'
```

The
[operator tmux architecture](operator-tmux-architecture.md)
documentation explains how workspace files are built and loaded. The public
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository documents the
[managed user schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#managed-user-entries)
for enabling auto-attach and selecting workspace files.

[Back to top](#operations)

## Grafana Cloud Observability

Grafana Cloud observability is configured by normal convergence when enabled
for production. To apply observability changes, run the normal manual runner
from the active control bastion:

```bash
sudo systemctl start grayhaven-ansible-runner.service
```

Useful local status commands:

```bash
sudo systemctl status alloy.service
sudo systemctl status grayhaven-observability-textfile.timer
sudo systemctl status grayhaven-irm-alert-groups-textfile.timer
sudo journalctl -u alloy.service
sudo journalctl -u grayhaven-ansible-runner.service
```

Ansible-managed Grafana alert rules use the managed label set
`configured_by=ansible`, `client=grayhaven`, and `environment=prod`. The
`client` value is derived from the managed host `client-*` inventory tag, and
Grafana Cloud alerting is currently supported only in `prod`. Manual Grafana
Cloud alert rules should not use that managed label set because Ansible uses it
to identify rules it owns.

Managed threshold and probe alerts treat missing query data as OK. Dedicated
metrics-data alerts fire when an expected host stops sending its `up` metric to
Grafana Cloud, such as when Alloy is stopped, blocked, or unable to ship host
metrics. This keeps telemetry gaps separate from CPU, memory, service, backup,
and web check alerts.

Treat a metrics-data alert as the primary incident for the affected host. Until
the host is sending usable metrics again, the affected host's other threshold
alerts remain normal on missing data and cannot be relied on to describe the
underlying failure.

Before planned maintenance, including managed host reboots, create a Grafana
Cloud silence for the affected managed alerts when Grafana Cloud is enabled.
Set the silence to expire at the planned end of the maintenance window. If the
window must be extended, extend the silence before it expires. If maintenance
finishes early, revoke the silence early so new issues are reported normally.
While silenced, matching alert groups and notifications are not created in
Grafana Cloud.

Useful silence matcher examples:

- All Ansible-managed production alerts:
  `configured_by=ansible`, `client=grayhaven`, `environment=prod`.
- All managed web server alerts:
  `configured_by=ansible`, `client=grayhaven`, `environment=prod`,
  `role=web`.
- A single managed web server:
  `configured_by=ansible`, `client=grayhaven`, `environment=prod`,
  `host=example-app-prod-web-01.example.com`.
- A single managed domain:
  `configured_by=ansible`, `client=grayhaven`, `environment=prod`,
  `domain=example.com`.

Grafana Cloud integration is supported only for the `prod` environment. The
[observability architecture](observability-architecture.md) documentation
describes what Ansible manages and what remains manual in Grafana Cloud.

[Back to top](#operations)

## Infrastructure Policy Changes

Active control bastion selection and web TLS mode are defined in
[`grayhaven-infra-opentofu`](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu)
policy. Changes to these settings must be applied in the
`grayhaven-infra-opentofu` repository. After applying the target workspace, run
a manual configuration pass from the active control bastion.

See the
[bastion failover documentation](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/operations.md#bastion-failover)
and
[TLS mode documentation](https://github.com/grayhavensystemsllc/grayhaven-infra-opentofu/blob/main/docs/operations.md#updating-workspace-environment-tls-mode)
in the
`grayhaven-infra-opentofu`
repository.

[Back to top](#operations)
