"""Every runtime asset ships with the distribution (templates, static, catalogs)."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'almali_library_curator'


def test_every_template_static_and_catalog_file_is_package_data():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    patterns = config['tool']['setuptools']['package-data']['almali_library_curator']
    assets = [path for folder in ('templates', 'static', 'i18n', 'web/templates')
              for path in (PACKAGE / folder).rglob('*') if path.is_file() and '__pycache__' not in path.parts]
    assert assets
    # Path.glob follows setuptools' rules: '*' stays in one directory, '**' recurses.
    shipped = {path for pattern in patterns for path in PACKAGE.glob(pattern)}
    missing = [str(path.relative_to(PACKAGE)) for path in assets if path not in shipped]
    assert missing == []
