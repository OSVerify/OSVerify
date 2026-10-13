"""Argument helpers shared by the core CLI and the relocated skill runtimes."""
from __future__ import annotations

import argparse


class ExplicitOption(argparse.Action):
    """Store a value and record that the user supplied it (defaults stay role-specific)."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.explicit_options = frozenset({*getattr(namespace, "explicit_options", ()), self.dest})
