"""Build a QGIS-installable archive without local Python bytecode."""
from configparser import ConfigParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
source = root / 'plugins/bdgd_aneel_manager'
metadata = ConfigParser()
metadata.read(source / 'metadata.txt', encoding='utf-8')
target = root / f'bdgd_aneel_manager_{metadata["general"]["version"]}.zip'
with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            archive.write(path, path.relative_to(source.parent))
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert 'bdgd_aneel_manager/metadata.txt' in archive.namelist()
print(target)
