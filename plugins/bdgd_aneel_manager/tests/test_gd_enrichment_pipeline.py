import csv
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bdgd_aneel_manager.analyses.panel_enrichment import enrich_panel


class GdEnrichmentPipelineTests(unittest.TestCase):
    def test_verified_source_keeps_network_keys_for_non_generating_consumers(self):
        row = dict(COD_ID='C1',source_year='2025',PN_CON='P1',PAC='A1',CTMT='F1',UNI_TR_MT='T1',
                   **{f'ENE_{m:02d}': m for m in range(1,13)})
        columns = [k for k in row if k != 'source_year']
        class Dataset:
            def ExecuteSQL(self, sql):
                if sql.startswith('SELECT *'):
                    return SimpleNamespace(schema=[SimpleNamespace(name=k.lower()) for k in columns])
                if sql.startswith('SELECT'):
                    # Recover the projected aliases to emulate GDAL result-field order.
                    import re
                    aliases = re.findall(r'AS "([A-Z_0-9]+)"',sql)
                    return [SimpleNamespace(GetField=lambda i: row[aliases[i]])]
                return None
            def ReleaseResultSet(self, layer):
                pass

        def verify_gd(dataset, entry, cols, rows, records, verified):
            self.assertEqual(records[0]['PN_CON'],'P1')
            self.assertEqual(records[0]['CTMT'],'F1')
            self.assertEqual(records[0]['UNI_TR_MT'],'T1')
            self.assertEqual(verified,{'C1'})
            return {'available':False,'year':2025},None

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'panel.csv'
            with path.open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(row))
                writer.writeheader(); writer.writerow(row)
            original=path.read_bytes()
            with patch('osgeo.gdal.OpenEx',return_value=Dataset()), \
                 patch('bdgd_aneel_manager.analyses.panel_enrichment.enrich_gd_year',side_effect=verify_gd):
                output, metadata=enrich_panel(path,'service=test',{'tables':[{'year':2025,'schema':'test','table':'ucbt_tab'}]})
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(metadata['schema_version'],3)
            self.assertTrue(output.exists())
