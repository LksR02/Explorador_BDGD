"""Run with OSGeo4W/bin/python-qgis.bat scripts/test_plugin.py."""
import sys
import os
import tempfile
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = Path(os.environ.get('OSGEO4W_ROOT', Path(sys.executable).resolve().parents[2]))
os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'))
dll_handles = [os.add_dll_directory(str(p)) for p in
               (runtime / 'bin', runtime / 'apps/Qt5/bin', runtime / 'apps/qgis/bin') if p.is_dir()]
test_temp = root / 'logs/test_tmp'
test_temp.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(test_temp)
sys.path.insert(0, str(root / 'plugins'))
suite = unittest.defaultTestLoader.discover(str(root / 'plugins/bdgd_aneel_manager/tests'))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
