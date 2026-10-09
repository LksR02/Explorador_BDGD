import tempfile
import unittest
from pathlib import Path

from bdgd_aneel_manager.catalog import load_catalog, parse_row
from bdgd_aneel_manager.plugin import human_size


class CatalogTests(unittest.TestCase):
    def test_qgis_plugin_module_imports(self):
        self.assertEqual(human_size(1011519298), "964.7 MiB")

    def test_friendly_name(self):
        item = parse_row({
            "id": "abc",
            "title": "Sulgipe_46_2025-12-31_V11_20260830-1351",
            "type": "File Geodatabase",
            "tags": "BDGD,SIG-R,Distribuicao,Extra,SULGIPE,2025-12-31",
            "modified": "Tue, 01 Sep 2026 08:03:09 GMT",
        })
        self.assertEqual(item.friendly_name, "SULGIPE_2025")

    def test_keeps_newest_version(self):
        content = (
            "id,title,type,tags,modified\n"
            'old,Enel_RJ_383_2025-12-31_V10_1,File Geodatabase,"BDGD,ENEL_RJ,2025-12-31","Mon, 31 Aug 2026 00:00:00 GMT"\n'
            'new,Enel_RJ_383_2025-12-31_V11_2,File Geodatabase,"BDGD,ENEL_RJ,2025-12-31","Tue, 01 Sep 2026 00:00:00 GMT"\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.csv"
            path.write_text(content, encoding="utf-8")
            items = load_catalog(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "new")
        self.assertEqual(items[0].friendly_name, "ENEL_RJ_2025")

    def test_prefers_year_end_over_later_uploaded_midyear_item(self):
        content = (
            "id,title,type,tags,modified\n"
            'mid,CHESP_103_2021-05-31_M10_1,File Geodatabase,"BDGD,CHESP,2021-05-31","Tue, 14 Oct 2025 00:00:00 GMT"\n'
            'annual,CHESP_103_2021-12-31_V10_2,File Geodatabase,"BDGD,CHESP,2021-12-31","Thu, 09 Oct 2025 00:00:00 GMT"\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.csv"
            path.write_text(content, encoding="utf-8")
            items = load_catalog(path)
        self.assertEqual(items[0].item_id, "annual")
        self.assertEqual(items[0].reference_date, "2021-12-31")


if __name__ == "__main__":
    unittest.main()
