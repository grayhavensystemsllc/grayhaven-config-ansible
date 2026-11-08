# Observability Architecture

Grayhaven Systems LLC production hosts can optionally publish metrics, logs, and
managed alert rules to Grafana Cloud.

## Table of Contents

- [Design](#design)
- [Enablement](#enablement)
- [Metrics](#metrics)
- [Pacific Billing-Month Usage Contract](#pacific-billing-month-usage-contract)
- [Logs](#logs)
- [Managed Alerts](#managed-alerts)
- [Operational Boundaries](#operational-boundaries)

## Design

Observability is configured by Ansible because it depends on runtime host
inventory, vault values, managed services, and files created during
convergence. OpenTofu applies the DigitalOcean tags that decide whether a
production host should participate.

Grafana Cloud is supported only for the `prod` environment at this time. If it
is enabled in another environment, convergence fails before installing or
syncing observability components.

The DigitalOcean metrics agent remains installed on managed hosts. Those
metrics are useful as a secondary provider-side view when Grafana Cloud is not
enabled, but Grafana Cloud is the primary alerting path when enabled.

[Back to top](#observability-architecture)

## Enablement

`grayhaven-vault` supplies the Grafana Cloud endpoint and credential values.
The public
[grayhaven-vault-example](https://github.com/grayhavensystemsllc/grayhaven-vault-example)
repository documents the expected
[configuration schema](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/schema.md#observability)
and
[Grafana Cloud setup](https://github.com/grayhavensystemsllc/grayhaven-vault-example/blob/main/docs/grafana-cloud-setup.md).

When Grafana Cloud is enabled, Ansible expects production hosts to carry the
`alerts-in-grafana-cloud` tag. Log shipping additionally requires
`logs-to-grafana-cloud`.

[Back to top](#observability-architecture)

## Metrics

Ansible installs Grafana Alloy on tagged production hosts and configures it to
publish Prometheus-compatible metrics to Grafana Cloud.

Metrics include:

- host CPU, memory, swap, filesystem, and disk statistics;
- managed systemd service state;
- Grayhaven Systems LLC host inventory metadata;
- active control-node metadata;
- Ansible convergence status;
- fail2ban jail status and ban counts;
- Time Tracker service state and authentication-ban status;
- restic backup, integrity-check, retention, and restore-size status, with
  subsecond precision for backup and integrity-check durations;
- Google Cloud Storage public service-health status, Pacific billing-month API
  operation totals, and daily stale restic bucket state;
- Cloud Monitoring public service-health status and Pacific billing-month billed
  query series;
- Proton public service-health status for Grayhaven Systems LLC-used services;
- selected OpenAI and DigitalOcean global/NYC3 public service-health components;
- sanitized active Grafana IRM alert-group state;
- HTTP, HTTPS, redirect, basic-auth, and certificate probes for configured web
  domains and the Time Tracker application.

HTTPS availability and certificate-expiry probes establish TLS while allowing
untrusted certificate chains and accepting any HTTP response status. Separate
certificate-trust probes validate the requested hostname and public trust while
also accepting any HTTP response status. This keeps staging-certificate
deployments from looking unavailable while still surfacing explicit
untrusted-certificate alerts. The local Time Tracker health probe remains a
separate HTTP 200 and JSON-health check.

The active control node publishes the full known-host inventory as textfile
metrics so dashboards and alert rules can reason about all expected hosts.
Google usage follows the Pacific billing-month contract below. The collector
queries Google Cloud Storage operation totals only for the expected restic
buckets and caches them for one hour. It republishes the cached totals during
its normal one-minute cycle so Grafana retains fresh local telemetry without
repeatedly querying Cloud Monitoring. Cloud Monitoring billed query series are
independently cached for one hour; that query uses server-side reduction to
return a single series. Managed alerts report stale usage telemetry and usage
that reaches the configured monthly warning threshold. Separate managed alerts
report public service-health collection failure and degraded Cloud Monitoring
status.
When Grafana Cloud is enabled, the active control node also publishes sanitized
Grafana IRM alert-group state as textfile metrics for operational reporting.
The collector reads only current alert-group metadata and read-only user
metadata needed to display acknowledgement ownership. It does not expose the
API token or raw IRM payloads in generated metrics.

[Back to top](#observability-architecture)

## Pacific Billing-Month Usage Contract

Google usage windows use Pacific Time (`America/Los_Angeles`), independent of
the host timezone and UTC calendar dates. For each collection, the window
starts at 00:00:00 Pacific on the first day of the current billing month and
ends at collection time. The collector converts both endpoints to UTC for the
Google APIs and labels the result with the Pacific month in `YYYY-MM` form.
This contract also handles Pacific daylight-saving transitions without changing
the month identity. Because aligned Google responses can include a point whose
interval begins before the requested boundary, the collector includes only
points with valid intervals that begin at or after the boundary. Malformed or
otherwise unusable intervals are ignored.

The two canonical textfile gauge metrics are:

- `grayhaven_gcs_restic_billing_month_operations_total`, with the Pacific
  billing month and GCS operation class labels for expected restic buckets;
- `grayhaven_google_monitoring_billing_month_series_total`, with the Pacific
  billing month label for the reduced Cloud Monitoring billed-query-series
  total.

Each cache records a schema version, project, billing month, refresh timestamp,
and typed usage values. The GCS cache also records the sorted expected bucket
set. GCS operation values are accumulated, cached, and rendered only for Class
A and Class B; Free and unrecognized methods are ignored. A cache is accepted
only when all of those identity and schema checks
match the current collection. At a billing boundary, the month mismatch
rejects the prior month's cache and forces a query for the new window. A
malformed, incompatible, wrong-project, wrong-month, or wrong-bucket cache is
therefore ignored rather than reused. If a same-month refresh fails, a valid
cache may be retained temporarily, but its freshness signal becomes stale
after the configured three-hour window. If no valid same-month cache exists,
collection fails closed and the corresponding usage metric is not emitted.

Managed Grafana usage rules query the raw canonical values with PromQL label
selection and aggregation; numeric thresholds are not embedded in PromQL.
Grafana evaluates each configured non-negative integer threshold on the query
result, using strict `greater than threshold minus one` semantics to represent
the exact inclusive boundary. Usage-threshold alerts own the usage value only.
Their NoData state is `OK`; collection-success and telemetry-staleness alerts
own missing or stale telemetry and use `Alerting` for NoData. This keeps a
telemetry gap from being mistaken for a usage-limit breach.

For durable verification, confirm that collected usage has the canonical metric
name and current Pacific `month` label, that the managed rule retains a raw
PromQL expression and exact integer Grafana-side threshold, and that usage and
telemetry-health NoData ownership remain separate.

[Back to top](#observability-architecture)

## Logs

Log shipping is optional. When enabled, Ansible configures Alloy to publish
selected logs to Grafana Cloud Loki.

The log set is intentionally targeted:

- Nginx access and error logs for configured domains;
- Time Tracker Nginx logs with shared-report tokens redacted at the source;
- MCP Nginx access and error logs with transfer capabilities and query strings
  redacted;
- audit logs;
- Certbot logs;
- systemd journal entries, including sudo and `grayhaven-root-command` command
  audit entries and structured Time Tracker application events, MCP Systems
  emissions, integration health, and collector status.

MCP operational logs use separate structured journal loggers. They do not record
tool arguments or results. Journal retention and the existing nginx logrotate
policy manage local cleanup; MCP has no separate application log directory.

Cloud-init logs are excluded. Shipped log lines pass through a defensive
redaction stage for common secret-like key names before leaving the host, but
redaction is a safeguard, not a handling procedure. Administrators should still
avoid placing secrets directly on command lines or in log output. If a secret
is exposed, rotate it immediately.

Some log streams are duplicated into derived operational views with additional
labels or concise line formatting while preserving the original selected log
streams. Sudo session-open/session-close noise and Ansible automation sudo
command lines are filtered from human-focused helper views so operator activity
remains easier to review. Ansible runner service logs are still shipped when log
shipping is enabled, and the runner also writes an ordered dashboard log stream
whose line numbers preserve playbook output order for Grafana log views.
Some textfile metrics are similarly derived from external operational state to
support concise status reporting without exposing raw service payloads.

[Back to top](#observability-architecture)

## Managed Alerts

Managed rules use a minimum five-minute pending period to tolerate brief
failures, except the MCP health check, which uses one minute. Existing longer
rule-specific periods are preserved. Query errors remain configured as errors
so sustained failures remain visible.

The active control node syncs Grafana Cloud alert rules during convergence.
Managed alert rules are labeled `configured_by=ansible`; Ansible only creates,
updates, or deletes rules carrying that label for the configured client. Manual
Grafana Cloud alert rules are left alone as long as they do not use that label.
Grafana Cloud does not allow API-provisioned alert rules to be modified or
deleted through the Grafana Cloud web interface, so managed alert rule changes
must go through Ansible. For planned maintenance, use Grafana Cloud silences as
documented in
[Operations](operations.md#grafana-cloud-observability).

On first convergence for a new control node, Ansible creates a short Grafana
Cloud silence matching the managed alert labels `configured_by=ansible`,
the configured client and environment labels before syncing managed alert rules.
The silence gives new hosts time to settle and gives Alloy time to send initial
telemetry before alert evaluation begins. A local marker prevents normal
convergence from creating the silence again on the same control node.

Managed alerts cover the same operational checks surfaced by the Grafana
dashboards where alerting is useful, including host metrics, service state,
backup freshness, Ansible convergence, web and Time Tracker availability,
development basic-auth behavior, certificate expiration,
certificate-expiration warning, certificate trust, and external service-health
state for Google Cloud Storage, Cloud Monitoring, Proton, OpenAI, and
DigitalOcean. Selected OpenAI and DigitalOcean components are configured in
`observability_public_status_services` and collected by the active control node
on its normal one-minute cycle. Collection failures have separate alerts.
Certificate-expiration warnings fire
when a certificate is valid but expires within 14 days; an expiry at or before
the current time is handled by the separate expired-certificate alert. CPU,
memory, filesystem, inode, and swap thresholds include the configured boundary
exactly. CPU alerts use a five-minute average and require five minutes at or
above threshold before firing. They are suppressed during Ansible convergence
and for 15 minutes afterward. A separate alert reports missing runner-state
telemetry; CPU alerts continue evaluating when that telemetry is missing.
External service-health alerts require fifteen minutes of degradation before
firing. Alert rules send to the configured Grafana IRM contact point.

Normal threshold and probe alerts treat missing query data as OK so a telemetry
gap does not make every managed alert fire at once. Per-host metrics-data
alerts are the dedicated reachability and telemetry health checks. They fire
when the expected host `up` metric is missing or below threshold, such as when
Alloy is stopped, blocked, or unable to send host metrics to Grafana Cloud.
While a host is not sending usable metrics, that host's other managed threshold
alerts remain normal on missing data. A metrics-data alert therefore means the
on-call operator should first restore or investigate the host telemetry path;
it has priority over downstream host-specific checks until metrics are flowing
again.

[Back to top](#observability-architecture)

## Operational Boundaries

Grafana dashboards, on-call schedules, escalation chains, outgoing webhooks,
and notification templates are configured in Grafana Cloud. They are not
managed by this repository.

Changing Grafana Cloud enablement or log shipping requires a normal Ansible
convergence run. When alert rules are updated, the sync helper honors Grafana
Cloud API rate-limit responses before retrying.

Alert definitions and stable UID overrides are supplied by the production Vault
branch. The sync helper renders them using generic helpers and a context without
API credentials. An alert definition can provide an optional
`annotation_expression` to expose a diagnostic instant query as reference `B`.
The alert condition continues to use the primary query `A`; annotations can
read the diagnostic result through `$values.B`.

Host process measurements count kernel tasks and zombies without reading
process arguments or environments. Container task measurements use each
configured container's cgroup PID count and limit.

[Back to top](#observability-architecture)
