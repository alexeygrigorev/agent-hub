"""Resolve installed package versions from the reviewed host release pins."""
import re

from . import bootstrap_pins


_VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\Z')


def package_version(provider, expected_package=None):
    """Return a provider's pinned package version without duplicating its pin."""
    spec = bootstrap_pins.ENGINE_PACKAGES.get(provider)
    if not isinstance(spec, str):
        raise ValueError('Invalid pinned engine package')
    package, separator, version = spec.rpartition('@')
    if (not separator or not package or not _VERSION.fullmatch(version)
            or expected_package is not None and package != expected_package):
        raise ValueError('Invalid pinned engine package')
    return version
