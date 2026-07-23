import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent
DEPLOY_FILES = (
    ROOT / 'deploy.sh',
    ROOT / 'deploy' / 'deploy.sh',
    ROOT / 'docker-compose.yaml',
)


@pytest.mark.parametrize('path', DEPLOY_FILES)
def test_deploy_sources_never_embed_live_credentials(path):
    text = path.read_text(encoding='utf-8')

    assert not re.search(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}\b', text)
    assert 'root1234' not in text
    assert 'CHANGE-ME-strong' not in text


@pytest.mark.parametrize('relative', ('deploy.sh', 'deploy/deploy.sh'))
def test_deploy_keeps_generated_env_files_private(relative):
    text = (ROOT / relative).read_text(encoding='utf-8')

    assert 'chmod 600 "$' in text
    assert 'bootstrap_admin --' not in text


def test_unconfigured_bot_is_parked_without_a_restart_loop():
    compose = (ROOT / 'docker-compose.yaml').read_text(encoding='utf-8')

    assert '$${CUSTOMER_BOT_TOKEN:-}' in compose
    assert 'exec tail -f /dev/null' in compose


def test_env_heredoc_cannot_execute_comment_substitutions():
    """An unquoted heredoc expands backticks even when they look commented."""
    text = (ROOT / 'deploy.sh').read_text(encoding='utf-8')
    env_block = text.split('cat > "$DIR/.env" <<EOF', 1)[1].split(
        '\nEOF',
        1,
    )[0]

    assert '`' not in env_block


def test_root_deploy_restarts_once_after_its_checkout_advances():
    """Never finish a release with the stale deploy script loaded by Bash."""
    text = (ROOT / 'deploy.sh').read_text(encoding='utf-8')

    assert 'DEPLOY_HEAD_BEFORE=' in text
    assert 'DEPLOY_HEAD_AFTER=' in text
    assert 'ALPHAPOS_DEPLOY_REEXEC:-0' in text
    assert 'exec env ALPHAPOS_DEPLOY_REEXEC=1 bash "$DIR/deploy.sh" "$IP"' in text
