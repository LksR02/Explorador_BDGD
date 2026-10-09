"""Provision an application-owned Windows PostgreSQL/PostGIS without admin rights.

No QGIS imports: the worker can be tested independently of the interface.
Existing clusters are never recreated and unrelated PostgreSQL services are not
started, stopped or reconfigured. Binaries and data live outside the plugin ZIP.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from zipfile import ZipFile


PG_VERSION = '17'
PG_URL = 'https://get.enterprisedb.com/postgresql/postgresql-17.11-4-windows-x64-binaries.zip'
PG_SHA256 = 'b9424ee7bc60b52450ff910a3630225df32e633f3cb29c1d126d9299d59aea28'
POSTGIS_URLS = (
    'https://download.osgeo.org/postgis/windows/pg17/postgis-bundle-pg17-3.6.2x64.zip',
    'https://ftp2.osuosl.org/pub/osgeo/download/postgis/windows/pg17/postgis-bundle-pg17-3.6.2x64.zip',
)
# Publisher's HTTPS checksum; a SHA-256 pin is also checked after release testing.
POSTGIS_MD5 = 'faca768cc580c4ab2eef621be05b408e'
POSTGIS_SHA256 = '7ba180ee2a352987b9a2f194673652c59483b55852295ccf401dceccd8765425'
CONNECTION_NAME = 'BDGD automático'


class DatabaseError(RuntimeError):
    """A user-readable failure which must not include credentials."""


class PreparationCancelled(DatabaseError):
    pass


def default_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'ExploradorBDGD/postgres'


def default_passfile():
    if os.environ.get('PGPASSFILE'):
        return Path(os.environ['PGPASSFILE'])
    return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'postgresql/pgpass.conf'


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def file_lock(path, cancelled=lambda: False, progress=lambda *_: None):
    """OS-held lock: closing/crashing releases it; no stale PID lock deletion."""
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + 900
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if cancelled():
                    raise PreparationCancelled('Preparação interrompida. Abra o plugin para continuar.')
                if time.monotonic() > deadline:
                    raise DatabaseError('Outra janela está preparando o banco. Tente novamente em instantes.')
                progress('Aguardando a preparação em outra janela do QGIS…', -1)
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def extract_runtime(archive, target, kind, cancelled=lambda: False):
    """Extract only server files, checking every ZIP path before any write."""
    target = Path(target).resolve()
    with ZipFile(archive) as zipped:
        members = []
        for info in zipped.infolist():
            name = info.filename.replace('\\', '/')
            parts = PurePosixPath(name).parts
            if not parts or name.startswith('/') or any(p in ('..', '.') or ':' in p for p in parts):
                raise DatabaseError('Pacote de instalação contém um caminho inválido.')
            if stat.S_ISLNK(info.external_attr >> 16):
                raise DatabaseError('Pacote de instalação contém um link inválido.')
            # EDB uses pgsql/; OSGeo uses postgis-bundle-pg17-.../.
            if parts[0] == 'pgsql' or parts[0].startswith('postgis-bundle-'):
                parts = parts[1:]
            if not parts or info.is_dir():
                continue
            if parts[0] not in ('bin', 'lib', 'share'):
                # Keep redistributable licensing/readmes beside the binaries.
                if len(parts) != 1 or not any(s in parts[0].lower() for s in ('license', 'copyright', 'readme', 'version')):
                    continue
            dest = target.joinpath(*parts).resolve()
            if not dest.is_relative_to(target):
                raise DatabaseError('Pacote de instalação contém um caminho inválido.')
            members.append((info, dest))
        for info, dest in members:
            if cancelled():
                raise PreparationCancelled('Preparação interrompida. Abra o plugin para continuar.')
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as source, dest.open('wb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)


def checksum(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class ManagedDatabase:
    def __init__(self, root=None, *, legacy_root=None, passfile=None,
                 progress=None, cancelled=None):
        self.root = Path(root or default_root()).resolve()
        self.legacy_root = Path(legacy_root).resolve() if legacy_root else None
        self.passfile = Path(passfile or default_passfile()).resolve()
        self.progress = progress or (lambda *_: None)
        self.cancelled = cancelled or (lambda: False)
        self.state = None

    def check_cancelled(self):
        if self.cancelled():
            raise PreparationCancelled('Preparação interrompida. Abra o plugin para continuar.')

    def prepare(self):
        if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
            raise DatabaseError('O preparo automático está disponível para Windows de 64 bits. Arquivos da ANEEL continuam disponíveis.')
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.root / '.prepare.lock', self.cancelled, self.progress):
            self.check_cancelled()
            self.state = self._load_state()
            if not self._runtime_valid():
                self._install_runtime()
            self._initialize()
            # Password is kept in a user-only directory, never in QGIS settings.
            self._register_passfile()
            self.check_cancelled()
            self.progress('Iniciando o banco local…', -1)
            self._start()
            self.progress('Habilitando PostGIS e verificando a conexão…', -1)
            self._ensure_database()
            self._save_state()
            self.progress('Banco local pronto. PostgreSQL e PostGIS configurados automaticamente.', 100)
            return {k: self.state[k] for k in ('host', 'port', 'database', 'username', 'runtime', 'data')} | {
                'connection_name': CONNECTION_NAME,
                'legacy_root': str(Path(self.state['data']).parent.parent) if self.state['legacy'] else '',
            }

    def _load_state(self):
        state_path = self.root / 'state.json'
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if state['format'] != 1 or state['host'] != '127.0.0.1' or state['database'] != 'bdgd' or state['username'] != 'bdgd_admin':
                    raise ValueError()
                for key in ('runtime', 'data', 'password_file'):
                    if not Path(state[key]).is_absolute():
                        raise ValueError()
                if not 1024 <= int(state['port']) <= 65535:
                    raise ValueError()
                return state
            except (ValueError, KeyError, TypeError):
                raise DatabaseError('O registro do banco local está inválido. Os dados foram preservados; consulte o suporte.') from None
        legacy = self.legacy_root
        if not legacy and self.root == default_root().resolve():
            legacy = next((p for p in Path(__file__).resolve().parents
                           if (p / 'runtime/pgdata/PG_VERSION').is_file()
                           and (p / '.secrets/postgres_password.txt').is_file()), None)
        if legacy and (legacy / 'runtime/pgdata/PG_VERSION').is_file() and (legacy / '.secrets/postgres_password.txt').is_file():
            self.progress('Reutilizando o banco BDGD existente; seus dados serão preservados…', -1)
            return dict(format=1, host='127.0.0.1', port=55432, database='bdgd', username='bdgd_admin',
                        runtime=str(legacy / 'runtime/pgsql'), data=str(legacy / 'runtime/pgdata'),
                        password_file=str(legacy / '.secrets/postgres_password.txt'), legacy=True)
        return dict(format=1, host='127.0.0.1', port=self._free_port(), database='bdgd', username='bdgd_admin',
                    runtime=str(self.root / 'pgsql'), data=str(self.root / 'data'),
                    password_file=str(self.root / 'secrets/password.txt'), legacy=False)

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', 55432))
                return 55432
            except OSError:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]

    def _save_state(self):
        atomic_text(self.root / 'state.json', json.dumps(self.state, indent=2))

    def _runtime_valid(self):
        runtime = Path(self.state['runtime'])
        return all((runtime / p).is_file() for p in (
            'bin/postgres.exe', 'bin/initdb.exe', 'bin/psql.exe', 'bin/pg_ctl.exe',
            'lib/postgis-3.dll', 'share/extension/postgis.control'))

    def _download(self, urls, name, sha256=None, md5=None):
        cache = self.root / 'downloads'
        cache.mkdir(exist_ok=True)
        target = cache / name
        def valid():
            return target.is_file() and (not sha256 or checksum(target) == sha256) and (not md5 or checksum(target, 'md5') == md5)
        if valid():
            return target
        temporary = target.with_suffix('.part')
        for url in urls:
            self.check_cancelled()
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'ExploradorBDGD/0.21'})
                with urllib.request.urlopen(request, timeout=15) as response, temporary.open('wb') as output:
                    total = int(response.headers.get('Content-Length', 0))
                    done, last = 0, 0
                    while True:
                        self.check_cancelled()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        done += len(chunk)
                        if time.monotonic() - last > 0.3:
                            self.progress(f'Baixando {name}: {done // (1024*1024)} MiB' +
                                          (f' de {total // (1024*1024)} MiB' if total else ''), int(done * 100 / total) if total else -1)
                            last = time.monotonic()
                os.replace(temporary, target)
                if not valid():
                    target.unlink()
                    raise DatabaseError('A verificação do download falhou. Tente novamente para baixar um pacote íntegro.')
                return target
            except PreparationCancelled:
                raise
            except (urllib.error.URLError, OSError, DatabaseError):
                continue
        raise DatabaseError('Não foi possível baixar os componentes do banco. Verifique a internet ou o proxy e clique em Tentar novamente. Arquivos da ANEEL continuam disponíveis.')

    def _install_runtime(self):
        if self.state['legacy']:
            raise DatabaseError('A instalação portátil existente está incompleta. Os dados foram preservados; consulte o suporte.')
        self.progress('Preparando PostgreSQL/PostGIS no primeiro uso (aproximadamente 480 MiB de download)…', -1)
        if shutil.disk_usage(self.root).free < 3 * 1024**3:
            raise DatabaseError('São necessários pelo menos 3 GiB livres para preparar o banco. Libere espaço e clique em Tentar novamente.')
        pg = self._download((PG_URL,), 'postgresql.zip', PG_SHA256)
        gis = self._download(POSTGIS_URLS, 'postgis.zip', POSTGIS_SHA256 or None, POSTGIS_MD5)
        staging = self.root / 'pgsql.staging'
        staging.mkdir(exist_ok=True)
        self.progress('Instalando os componentes locais do banco…', -1)
        extract_runtime(pg, staging, 'postgres', self.cancelled)
        extract_runtime(gis, staging, 'postgis', self.cancelled)
        runtime = Path(self.state['runtime'])
        if runtime.exists():
            # Never delete a directory possibly containing user files/data.
            raise DatabaseError('A pasta dos componentes está incompleta. Os dados foram preservados; consulte o suporte.')
        staging.rename(runtime)
        if not self._runtime_valid():
            raise DatabaseError('Pacote incompatível: componentes de PostgreSQL/PostGIS ausentes.')

    def _environment(self):
        env = os.environ.copy()
        runtime = Path(self.state['runtime'])
        env['PATH'] = str(runtime / 'bin') + os.pathsep + str(runtime / 'lib') + os.pathsep + env.get('PATH', '')
        # Do not let unrelated PG* settings select someone else's database.
        for key in tuple(env):
            if key.startswith('PG'):
                env.pop(key)
        env['PGPASSFILE'] = str(self.passfile)
        env['PGCONNECT_TIMEOUT'] = '8'
        return env

    def _command(self, executable, arguments, *, timeout=90, checked=True):
        try:
            # On Windows, the detached server can inherit pg_ctl's output pipe.
            # Capturing that pipe would block until the server shuts down.
            output = subprocess.DEVNULL if executable == 'pg_ctl' else subprocess.PIPE
            result = subprocess.run([str(Path(self.state['runtime']) / 'bin' / (executable + '.exe')), *map(str, arguments)],
                                    env=self._environment(), stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise DatabaseError(f'Não foi possível executar {executable}. Verifique o bloqueio pelo antivírus e tente novamente.') from None
        if checked and result.returncode:
            # stdout can contain a connection string/password. Never surface it.
            raise DatabaseError(f'Falha ao executar {executable}. Os dados foram preservados. Consulte o registro local do PostgreSQL para diagnóstico.')
        return result.returncode, (result.stdout or b'').decode('utf-8', errors='replace').strip()

    def _initialize(self):
        data = Path(self.state['data'])
        version = data / 'PG_VERSION'
        password_file = Path(self.state['password_file'])
        if version.exists():
            if version.read_text().strip() != PG_VERSION:
                raise DatabaseError('O banco existente usa outra versão do PostgreSQL. Uma migração é necessária; os dados foram preservados.')
            if not password_file.is_file():
                raise DatabaseError('A credencial do banco existente não foi encontrada. Os dados foram preservados; consulte o suporte.')
            return
        if data.exists() and any(data.iterdir()):
            raise DatabaseError('A inicialização anterior está incompleta. Os arquivos foram preservados; consulte o suporte.')
        self.progress('Criando o banco local e suas credenciais…', -1)
        password_file.parent.mkdir(parents=True, exist_ok=True)
        self._protect_directory(password_file.parent)
        if not password_file.exists():
            atomic_text(password_file, secrets.token_urlsafe(36))
        self._save_state()  # Persist port and credentials before initializing.
        # initdb is not interrupted mid-operation: preserve a consistent cluster.
        # Initialize a separate, unique directory. A failed/cancelled first use
        # must not leave a partial cluster at the persistent data path.
        staging = self.root / ('initdb-' + secrets.token_hex(8))
        staging.mkdir()
        self._command('initdb', ['-D', staging, '-U', self.state['username'], '--encoding=UTF8', '--locale=C',
                                 '--auth-local=scram-sha-256', '--auth-host=scram-sha-256', '--pwfile', password_file])
        configuration = ("\n# Explorador BDGD: local, sem serviço do Windows\nlisten_addresses = '127.0.0.1'\n"
                         f"port = {self.state['port']}\nmax_connections = 30\nshared_buffers = '128MB'\n"
                         "maintenance_work_mem = '128MB'\nwork_mem = '16MB'\n")
        with (staging / 'postgresql.conf').open('a', encoding='utf-8') as handle:
            handle.write(configuration)
        # Empty data directory from an earlier attempt can be removed safely.
        if data.exists():
            data.rmdir()
        staging.rename(data)

    @staticmethod
    def _protect_directory(path):
        """Restrict newly created secret directory to this Windows account/SYSTEM."""
        try:
            identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                                      creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            sid = next(csv.reader(identity.stdout.decode('mbcs', errors='replace').strip().splitlines()))[1]
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            raise DatabaseError('Não foi possível proteger as credenciais na pasta do usuário.') from None

    def _register_passfile(self):
        password = Path(self.state['password_file']).read_text(encoding='utf-8').strip()
        if not password or '\n' in password or '\r' in password:
            raise DatabaseError('A credencial local está inválida. Os dados foram preservados.')
        escaped = password.replace('\\', '\\\\').replace(':', '\\:')
        # Standard libpq passfile is used by QGIS AND GDAL. Preserve other entries.
        self.passfile.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.passfile.parent / '.bdgd-passfile.lock', self.cancelled):
            old = self.passfile.read_text(encoding='utf-8') if self.passfile.exists() else ''
            entries = [f"{host}:{self.state['port']}:*:{self.state['username']}:{escaped}" for host in ('127.0.0.1', 'localhost')]
            existing = old.splitlines()
            content = '\n'.join(entries + [line for line in existing if line not in entries]) + '\n'
            if content != old:
                atomic_text(self.passfile, content)

    def _start(self):
        data = Path(self.state['data'])
        status, _ = self._command('pg_ctl', ['-D', data, 'status'], checked=False)
        if status:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', int(self.state['port']))) == 0:
                    if self.state['legacy']:
                        raise DatabaseError('A porta do banco existente está ocupada por outro processo. Os dados foram preservados.')
                    self.state['port'] = self._free_port()
                    self._save_state()
                    self._register_passfile()
            self._command('pg_ctl', ['-D', data, '-l', self.root / 'postgresql.log', '-w', '-t', '45',
                                     '-o', f"-p {self.state['port']}", 'start'], timeout=55)
        # Authenticate and verify the actual cluster, not merely an open port.
        actual = self._sql('SHOW data_directory', 'postgres')
        if os.path.normcase(str(Path(actual).resolve())) != os.path.normcase(str(data.resolve())):
            raise DatabaseError('A conexão respondeu por outro banco. Nenhum dado foi alterado.')

    def _sql(self, sql, database=None):
        return self._command('psql', ['-h', self.state['host'], '-p', self.state['port'], '-U', self.state['username'],
                                      '-d', database or self.state['database'], '-w', '-X', '-A', '-t',
                                      '-v', 'ON_ERROR_STOP=1', '-c', sql])[1]

    def _ensure_database(self):
        if self._sql("SELECT 1 FROM pg_database WHERE datname='bdgd'", 'postgres') != '1':
            self._sql('CREATE DATABASE bdgd', 'postgres')
        self._sql('CREATE EXTENSION IF NOT EXISTS postgis')
        self._sql('SELECT PostGIS_Version()')
        self._sql('''CREATE SCHEMA IF NOT EXISTS bdgd_admin;
          CREATE TABLE IF NOT EXISTS bdgd_admin.import_log (
            distribuidora text NOT NULL, ano integer NOT NULL, camada_origem text NOT NULL,
            schema_destino text NOT NULL, tabela_destino text NOT NULL, status text NOT NULL,
            started_at timestamptz DEFAULT now(), finished_at timestamptz, row_count bigint,
            error_message text, PRIMARY KEY(distribuidora, ano, camada_origem));''')
