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


def test_telegram_outbox_has_an_isolated_worker_and_persistent_media():
    compose = (ROOT / 'docker-compose.yaml').read_text(encoding='utf-8')

    assert 'smartfood_messages:' in compose
    assert 'process_smartfood_messages' in compose
    assert 'media_data:/app/private_media' in compose


@pytest.mark.parametrize('script', ('deploy.sh', 'deploy/deploy.sh'))
def test_deploy_scripts_use_one_customer_delivery_host(script):
    source = (ROOT / script).read_text(encoding='utf-8')

    assert 'DELIVERY_HOST="delivery.${IP}.nip.io"' in source
    assert 'DELIVERY_URL="https://${DELIVERY_HOST}/webapp/"' in source
    assert 'reverse_proxy smartfood-webapp:80' in source
    for legacy_host in ('pos', 'smartfood', 'webapp'):
        assert f'"https://{legacy_host}.${{IP}}.nip.io/webapp/"' in source


def test_self_redeploy_reconciles_the_customer_delivery_route():
    source = (ROOT / 'deploy' / 'auto_redeploy.sh').read_text(encoding='utf-8')
    reconcile = (ROOT / 'deploy' / 'reconcile_delivery.sh').read_text(
        encoding='utf-8',
    )

    assert 'reconcile_delivery.sh' in source
    assert 'CUSTOMER_WEBAPP_URL=' in reconcile
    assert 'getChatMenuButton' in reconcile
    assert 'setChatMenuButton' in reconcile
    assert 'smartfood-webapp' in reconcile
    assert 'CONFIGURED_DELIVERY_URL' in source
