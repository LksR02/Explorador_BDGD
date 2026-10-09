import json
from pathlib import Path
import secrets
import shutil
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from bdgd_aneel_manager.managed_database import (
    DatabaseError, ManagedDatabase, PreparationCancelled,
    atomic_text, checksum, extract_runtime,
)


class ManagedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[3] / 'logs' / ('db-unit-' + secrets.token_hex(6))
        self.root.mkdir(parents=True)
        self.manager = ManagedDatabase(self.root, passfile=self.root / 'client/pgpass.conf')

    def tearDown(self):
        # This unique fixture path is verified inside the workspace logs folder.
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parents[3] / 'logs'))
        shutil.rmtree(self.root)

    def test_zip_traversal_rejected_before_writing(self):
        archive = self.root / 'bad.zip'
        with ZipFile(archive, 'w') as output:
            output.writestr('pgsql/bin/good.exe', b'good')
            output.writestr('pgsql/../../outside.exe', b'bad')
        with self.assertRaises(DatabaseError):
            extract_runtime(archive, self.root / 'extracted', 'postgres')
        self.assertFalse((self.root / 'extracted').exists())

    def test_zip_symlink_rejected(self):
        archive = self.root / 'bad.zip'
        entry = ZipInfo('pgsql/bin/link.exe')
        entry.external_attr = 0o120777 << 16
        with ZipFile(archive, 'w') as output:
            output.writestr(entry, b'/outside')
        with self.assertRaises(DatabaseError):
            extract_runtime(archive, self.root / 'extracted', 'postgres')

    def test_only_server_files_and_licenses_are_extracted(self):
        archive = self.root / 'server.zip'
        with ZipFile(archive, 'w') as output:
            for name in ('bin/postgres.exe', 'lib/postgis-3.dll', 'share/extension/postgis.control', 'LICENSE', 'pgAdmin 4/giant.bin'):
                output.writestr('pgsql/' + name, b'fixture')
        extract_runtime(archive, self.root / 'extracted', 'postgres')
        self.assertTrue((self.root / 'extracted/bin/postgres.exe').exists())
        self.assertTrue((self.root / 'extracted/LICENSE').exists())
        self.assertFalse((self.root / 'extracted/pgAdmin 4').exists())

    def test_cancelled_extraction(self):
        archive = self.root / 'server.zip'
        with ZipFile(archive, 'w') as output:
            output.writestr('pgsql/bin/postgres.exe', b'fixture')
        with self.assertRaises(PreparationCancelled):
            extract_runtime(archive, self.root / 'extracted', 'postgres', lambda: True)

    def test_cached_download_must_match_pin(self):
        cache = self.root / 'downloads'
        cache.mkdir()
        (cache / 'runtime.zip').write_bytes(b'tampered')
        source = self.root / 'official.zip'
        source.write_bytes(b'official')
        downloaded = self.manager._download((source.as_uri(),), 'runtime.zip', checksum(source))
        self.assertEqual(downloaded.read_bytes(), b'official')

    def test_mismatched_download_is_never_installed(self):
        source = self.root / 'wrong.zip'
        source.write_bytes(b'wrong')
        with self.assertRaises(DatabaseError):
            self.manager._download((source.as_uri(),), 'runtime.zip', '0' * 64)
        self.assertFalse((self.root / 'downloads/runtime.zip').exists())

    def test_invalid_state_preserves_data(self):
        marker = self.root / 'data/keep'
        atomic_text(marker, 'precious')
        atomic_text(self.root / 'state.json', '{bad json')
        with self.assertRaises(DatabaseError):
            self.manager._load_state()
        self.assertEqual(marker.read_text(), 'precious')

    def test_wrong_cluster_major_is_never_reinitialized(self):
        self.manager.state = self.manager._load_state()
        atomic_text(Path(self.manager.state['data']) / 'PG_VERSION', '16')
        with patch.object(self.manager, '_command') as command:
            with self.assertRaises(DatabaseError):
                self.manager._initialize()
            command.assert_not_called()

    def test_passfile_preserves_existing_connections_and_is_idempotent(self):
        self.manager.state = self.manager._load_state()
        atomic_text(self.manager.state['password_file'], 'test:password\\value')
        atomic_text(self.manager.passfile, 'remote:5432:other:someone:original\n')
        self.manager._register_passfile()
        content = self.manager.passfile.read_text()
        self.assertIn('remote:5432:other:someone:original\n', content)
        self.assertIn('test\\:password\\\\value', content)
        self.manager._register_passfile()
        self.assertEqual(self.manager.passfile.read_text(), content)
        self.manager._save_state()
        self.assertNotIn('test:password', (self.root / 'state.json').read_text())

    def test_command_errors_do_not_disclose_credentials(self):
        self.manager.state = self.manager._load_state()
        from subprocess import CompletedProcess
        with patch('bdgd_aneel_manager.managed_database.subprocess.run', return_value=CompletedProcess([], 1, b'password=secret')):
            with self.assertRaises(DatabaseError) as error:
                self.manager._command('psql', [])
        self.assertNotIn('secret', str(error.exception))

    def test_pg_ctl_does_not_capture_a_server_inherited_pipe(self):
        self.manager.state = self.manager._load_state()
        from subprocess import CompletedProcess, DEVNULL
        with patch('bdgd_aneel_manager.managed_database.subprocess.run', return_value=CompletedProcess([], 0, None)) as run:
            self.assertEqual(self.manager._command('pg_ctl', ['status']), (0, ''))
        self.assertEqual(run.call_args.kwargs['stdout'], DEVNULL)

    def test_explicit_test_root_does_not_adopt_production_cluster(self):
        state = self.manager._load_state()
        self.assertFalse(state['legacy'])
        self.assertTrue(Path(state['data']).is_relative_to(self.root))
