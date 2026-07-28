"""Deterministic settings for the server test suite."""

import os

os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('SECRET_KEY', 'pytest-secret-key')
os.environ.setdefault(
    'LICENSE_FERNET_KEY',
    '6XzGcRmA0kcl-pX8R8wQbHCJqB7pDhVcMpC_Z8ZcKp4=',
)
os.environ.setdefault('CLOUD_DEFAULT_TARGET_BRANCH_ID', 'branch1')

from .settings import *  # noqa: E402,F401,F403

STATIC_ROOT = None
