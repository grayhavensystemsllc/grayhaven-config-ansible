"""Invented inputs for generic alert rendering and synchronization tests."""

import base64
import uuid

import yaml


RULE_TEMPLATE = """
{% set rules = [] %}
{% for check in parameters.checks %}
{% do rules.append(alert_rule(
    identity=check.identity, uid_registry=config.uid_registry,
    title=check.title, folder_uid=folder_uid,
    rule_group=config.grafana.evaluation_group, datasource_uid=datasource_uid,
    expression=check.expression, evaluator_type=check.comparison,
    threshold=check.threshold,
    labels=host_labels(config, config.hosts[0], check.identity),
    annotations={"summary": check.title},
    contact_point=config.grafana.contact_point
)) %}
{% endfor %}
{{ rules | tojson }}
"""


def complete_config():
    definitions = {
        "parameters": {
            "checks": [
                {
                    "identity": "sample:load",
                    "title": "Sample load",
                    "expression": "sample_load",
                    "comparison": "gt",
                    "threshold": 7.5,
                },
                {
                    "identity": "sample:capacity",
                    "title": "Sample capacity",
                    "expression": "sample_capacity",
                    "comparison": "lt",
                    "threshold": 3,
                },
            ],
        },
        "templates": [{"name": "Sample checks", "template": RULE_TEMPLATE}],
    }
    return {
        "grafana": {
            "api_token": uuid.uuid4().hex,
            "stack_url": "https://metrics.example.test",
            "folder": "Sample folder",
            "evaluation_group": "sample-group",
            "evaluation_interval": "1m",
            "contact_point": "Sample receiver",
            "prometheus_datasource_name": "Sample metrics",
        },
        "labels": {"client": "fixture", "environment": "sample"},
        "hosts": [
            {
                "fqdn": "worker.example.test",
                "short_hostname": "sample-worker",
                "role": "worker",
                "project": "sample-project",
                "control_node": True,
            }
        ],
        "sites": [],
        "public_status": {},
        "service_units": {},
        "thresholds": {},
        "tls_mode": "host",
        "uid_registry": {
            "sample:load": "gh-" + "1" * 24,
            "sample:capacity": "gh-" + "2" * 24,
        },
        "rule_definitions": definitions,
        "rule_definitions_base64": base64.b64encode(
            yaml.safe_dump(definitions).encode()
        ).decode(),
    }
