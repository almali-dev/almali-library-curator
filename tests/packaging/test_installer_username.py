"""Owner-name validation through the public Unraid installer (#33)."""
import pytest

from tests.packaging.test_unraid_installer import (
    docker_calls, fake_docker, run_installer,
)


@pytest.mark.parametrize('separator', ['\n', '\r'])
@pytest.mark.parametrize('existing', [False, True])
def test_username_line_break_is_refused_without_install_effects(tmp_path, fake_docker, separator, existing):
    appdata, media = tmp_path / 'appdata', tmp_path / 'media'
    media.mkdir()
    if existing:
        appdata.mkdir()
        (appdata / 'sentinel').write_bytes(b'unchanged')
    before = {p.name: p.read_bytes() for p in appdata.iterdir()} if existing else None
    response = run_installer(fake_docker, appdata, media,
                             ALC_INSTALL_USER=f'x{separator}PUID=0')
    assert response.returncode == 1
    assert 'The user name cannot contain a line break' in response.stderr
    assert 'Installation complete' not in response.stdout + response.stderr
    assert not any(c.startswith(('pull ', 'run ')) for c in docker_calls(fake_docker))
    if existing:
        assert {p.name: p.read_bytes() for p in appdata.iterdir()} == before
    else:
        assert not appdata.exists()
