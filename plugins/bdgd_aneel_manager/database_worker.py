"""Keep database preparation off the QGIS interface thread."""
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsApplication

from .managed_database import ManagedDatabase, DatabaseError


class DatabasePreparationThread(QThread):
    progress = pyqtSignal(str, int)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, legacy_root=None):
        # Closing/unloading a dialog must not destroy a running QThread.
        super().__init__(QgsApplication.instance())
        self.legacy_root = legacy_root
        QgsApplication.instance().aboutToQuit.connect(self.shutdown)

    def run(self):
        try:
            database = ManagedDatabase(legacy_root=self.legacy_root,
                                       progress=self.progress.emit,
                                       cancelled=self.isInterruptionRequested)
            self.succeeded.emit(database.prepare())
        except DatabaseError as exc:
            self.failed.emit(str(exc))
        except Exception:
            # No raw command output, paths or passwords in UI error messages.
            self.failed.emit('Não foi possível preparar o banco local. Os dados foram preservados. Tente novamente.')

    def shutdown(self):
        self.requestInterruption()
        self.wait()
