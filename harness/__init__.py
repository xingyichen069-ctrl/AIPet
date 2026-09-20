"""A small, isolated plugin lifecycle harness for AIPet."""

from .core import (
    HarnessError,
    PluginSpec,
    auto_use,
    discover_plugins,
    install_archive,
    package_plugin,
    run_plugin,
    run_plugin_test,
    validate_plugin,
)
from .editor import DraftResult, GuidedEditor, OpenAICompatClient

__all__ = [
    "HarnessError",
    "PluginSpec",
    "auto_use",
    "discover_plugins",
    "install_archive",
    "package_plugin",
    "run_plugin",
    "run_plugin_test",
    "validate_plugin",
    "DraftResult",
    "GuidedEditor",
    "OpenAICompatClient",
]
