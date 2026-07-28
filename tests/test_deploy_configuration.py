from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('script', ('deploy.sh', 'deploy/deploy.sh'))
def test_deploy_scripts_preserve_runtime_tuning(script):
    source = (ROOT / script).read_text(encoding='utf-8')

    for setting in (
        'WEB_CONCURRENCY',
        'OPENAI_SEED',
        'AI_TEMPERATURE',
    ):
        assert source.count(setting) >= 2


def test_compose_passes_web_concurrency_to_django_and_uvicorn():
    compose = (ROOT / 'docker-compose.yaml').read_text(encoding='utf-8')

    assert 'WEB_CONCURRENCY: ${WEB_CONCURRENCY:-3}' in compose
