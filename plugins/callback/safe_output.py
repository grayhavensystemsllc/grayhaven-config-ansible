"""Ansible display adapter; sanitization behavior lives in the tested Python helper."""

import importlib.machinery
import importlib.util
from pathlib import Path

from ansible.plugins.callback.default import CallbackModule as DefaultCallback
DOCUMENTATION = """
    name: safe_output
    type: stdout
    short_description: Ansible screen output with sensitive values removed
    description:
      - Removes sensitive values before displaying task output.
    extends_documentation_fragment:
      - default_callback
      - result_format_callback
    requirements:
      - set as stdout in configuration
"""
loader = importlib.machinery.SourceFileLoader(
    "grayhaven_output_redactor",
    str(Path(__file__).resolve().parents[2] / "files/grayhaven-redact-output"),
)
spec = importlib.util.spec_from_loader(loader.name, loader)
redaction = importlib.util.module_from_spec(spec)
loader.exec_module(redaction)


class CallbackModule(DefaultCallback):
    CALLBACK_NAME = "safe_output"

    def __init__(self):
        super().__init__()
        self.redactor = redaction.Redactor()
        self._display = redaction.SafeDisplay(self._display, self.redactor)

    def v2_playbook_on_play_start(self, play):
        self.redactor.secret_values.update(
            redaction.collect_secret_values(play.get_variable_manager().extra_vars)
        )
        return super().v2_playbook_on_play_start(play)

    def _dump_results(self, result, *arguments, **keywords):
        self.redactor.secret_values.update(redaction.collect_secret_values(result))
        return super()._dump_results(self.redactor.tree(result), *arguments, **keywords)
