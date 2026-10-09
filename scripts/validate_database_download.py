"""Check the exact Python HTTPS path used by the first-use worker."""
from pathlib import Path
import sys
import urllib.request

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'plugins'))
from bdgd_aneel_manager.managed_database import ManagedDatabase, PG_URL, POSTGIS_URLS, POSTGIS_MD5

manager = ManagedDatabase(root / 'logs/network-bootstrap-test')
manager.root.mkdir(exist_ok=True)
with urllib.request.urlopen(urllib.request.Request(PG_URL, method='HEAD'), timeout=20) as response:
    assert response.status == 200
    assert int(response.headers['Content-Length']) > 300 * 1024**2
artifact = manager._download(tuple(url + '.md5' for url in POSTGIS_URLS), 'publisher.md5')
assert artifact.read_text().split()[0] == POSTGIS_MD5
print('PASS: Python HTTPS / PostgreSQL EDB e PostGIS OSGeo, incluindo fallback do download.')
