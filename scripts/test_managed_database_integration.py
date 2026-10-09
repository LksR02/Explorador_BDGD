"""Real first-use, restart, conflict and QGIS/GDAL integration in an isolated DB.

Run using OSGeo4W/bin/python-qgis.bat. Uses previously downloaded official ZIPs;
all test data and credentials stay under logs/, never the production cluster.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import secrets
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
from bdgd_aneel_manager.managed_database import ManagedDatabase, PG_SHA256, POSTGIS_SHA256, checksum

if '--reuse' in sys.argv:
    root = Path(json.loads((ROOT / 'logs/managed_database_integration.json').read_text())['root']).resolve()
    assert root.is_relative_to((ROOT / 'logs').resolve()) and root.name.startswith('database-integration-')
else:
    root = ROOT / 'logs' / ('database-integration-' + secrets.token_hex(8))
    root.mkdir()
(root / 'downloads').mkdir(exist_ok=True)
for name, expected in [('postgresql.zip', PG_SHA256), ('postgis.zip', POSTGIS_SHA256)]:
    source = ROOT / 'logs/bootstrap_downloads' / name
    assert checksum(source) == expected, name
    if not (root / 'downloads' / name).exists():
        shutil.copyfile(source, root / 'downloads' / name)
os.environ['PGPASSFILE'] = str(root / 'client/pgpass.conf')
last = ''
def progress(message, percent):
    global last
    if message != last:
        print(message, flush=True)
        last = message

manager = ManagedDatabase(root, progress=progress)
app = None
try:
    result = manager.prepare()
    assert Path(result['data']).is_relative_to(root)
    assert manager._sql('SELECT PostGIS_Version()').startswith('3.6')
    manager._sql('CREATE TABLE IF NOT EXISTS public.bootstrap_marker (id int); INSERT INTO public.bootstrap_marker SELECT 42 WHERE NOT EXISTS (SELECT 1 FROM public.bootstrap_marker)')
    original = Path(result['data'])
    assert not any(k in json.loads((root / 'state.json').read_text()) for k in ('password', 'secret'))
    second = ManagedDatabase(root)
    assert second.prepare()['data'] == str(original)
    assert second._sql('SELECT id FROM public.bootstrap_marker') == '42'
    manager._command('pg_ctl', ['-D', original, '-w', 'stop'])
    third = ManagedDatabase(root)
    third.prepare()
    assert third._sql('SELECT id FROM public.bootstrap_marker') == '42'
    third._command('pg_ctl', ['-D', original, '-w', 'stop'])
    # A different process occupies the saved port. The app must preserve the DB
    # and select a different port instead of connecting to that process.
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', int(third.state['port'])))
        blocker.listen()
        fourth = ManagedDatabase(root)
        result = fourth.prepare()
        assert result['port'] != third.state['port']
        assert fourth._sql('SELECT id FROM public.bootstrap_marker') == '42'
    manager = fourth
    from qgis.core import QgsApplication, QgsProviderRegistry, QgsDataSourceUri, QgsVectorLayer
    from osgeo import gdal
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['QT_QPA_FONTDIR'] = str(Path(os.environ['WINDIR']) / 'Fonts')
    app = QgsApplication([], False)
    app.setOrganizationName('BDGDIntegration')
    app.setApplicationName('AutomaticDatabaseTest')
    app.initQgis()
    from qgis.PyQt.QtGui import QFont, QFontDatabase
    QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR']) / 'Fonts/segoeui.ttf'))
    app.setFont(QFont('Segoe UI', 9))
    uri = QgsDataSourceUri()
    uri.setConnection(result['host'], str(result['port']), result['database'], result['username'], '')
    provider = QgsProviderRegistry.instance().providerMetadata('postgres')
    conn = provider.createConnection(uri.uri(False), {})
    assert conn.executeSql('SELECT id FROM public.bootstrap_marker') == [[42]]
    conn.executeSql('CREATE TABLE IF NOT EXISTS public.spatial_marker (id integer PRIMARY KEY, geom geometry(Point,4326)); INSERT INTO public.spatial_marker VALUES (1, ST_SetSRID(ST_Point(-43,-22),4326)) ON CONFLICT DO NOTHING')
    uri.setDataSource('public', 'spatial_marker', 'geom', '', 'id')
    layer = QgsVectorLayer(uri.uri(False), 'Auto DB test', 'postgres')
    assert layer.isValid() and layer.featureCount() == 1
    dataset = gdal.OpenEx('PG:' + uri.connectionInfo(False), gdal.OF_VECTOR)
    assert dataset is not None
    cursor = dataset.ExecuteSQL('SELECT id FROM public.bootstrap_marker')
    assert cursor.GetNextFeature().GetField('id') == 42
    dataset.ReleaseResultSet(cursor)
    cursor, dataset, layer, conn = None, None, None, None
    from bdgd_aneel_manager.plugin import BdgdAneelPlugin, BdgdDialog
    from bdgd_aneel_manager.managed_database import CONNECTION_NAME
    class Interface:
        def mainWindow(self):
            return None
    def isolated_database(**kwargs):
        kwargs.pop('legacy_root', None)
        return ManagedDatabase(root, **kwargs)
    plugin = BdgdAneelPlugin(Interface())
    with patch('bdgd_aneel_manager.database_worker.ManagedDatabase', side_effect=isolated_database), patch.object(BdgdDialog, 'reload_catalog'):
        plugin.run()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            app.processEvents()
            if plugin.dialog.database_ready and plugin.dialog.postgres_browser.loaded_connection == CONNECTION_NAME:
                break
            time.sleep(0.03)
        assert plugin.dialog.database_ready, plugin.dialog.database_status.text()
        assert plugin.dialog.postgres_browser.loaded_connection == CONNECTION_NAME, plugin.dialog.postgres_browser.status.text()
        saved = provider.createConnection(CONNECTION_NAME)
        assert QgsDataSourceUri(saved.uri()).password() == ''
        assert QgsDataSourceUri(saved.uri()).username() == result['username']
        assert saved.executeSql('SELECT id FROM public.bootstrap_marker') == [[42]]
        plugin.dialog.grab().save(str(ROOT / 'logs/automatic_database_ui.png'))
        saved = None
        plugin.unload()
        app.processEvents()
    print('PASS: primeiro uso real, PostGIS, reabertura, reinício, conflito de porta, preservação dos dados, QGIS/GDAL sem senha salva e abertura automática pela interface.', flush=True)
    (ROOT / 'logs/managed_database_integration.json').write_text(json.dumps({'passed': True, 'root': str(root), 'postgres': '17.11', 'postgis': '3.6.2', 'checks': 10}, indent=2))
finally:
    if app:
        app.exitQgis()
    if manager.state and (Path(manager.state['data']) / 'PG_VERSION').exists():
        manager._command('pg_ctl', ['-D', manager.state['data'], '-w', 'stop'], checked=False)
