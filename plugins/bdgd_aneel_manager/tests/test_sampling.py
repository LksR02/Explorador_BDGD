import unittest
from unittest.mock import patch

import numpy as np

from bdgd_aneel_manager.analyses import demand_mvp as demand


class SamplingTests(unittest.TestCase):
    def test_grouping_matches_reference_including_empty_batch(self):
        for n in (0, 10000):
            rng = np.random.default_rng(42)
            strata = rng.integers(0, 400, n)
            valid = rng.random(n) > .15
            actual = list(demand._grouped_indexes(strata, valid))
            self.assertEqual([int(v) for v, _ in actual], list(np.unique(strata[valid])))
            for value, indexes in actual:
                np.testing.assert_array_equal(indexes, np.flatnonzero(valid & (strata == value)))

    def test_selection_weights_and_batch_invariance(self):
        n = 12000
        batch = {'COD_ID': np.array([str(i).encode() for i in range(n)]),
                 'GRU_TAR': np.array([f'B{i % 23}'.encode() for i in range(n)]),
                 'MUN': np.full(n, b'1')}
        batch.update({field: np.full(n, 50.0) for field in demand.MONTH_FIELDS})
        batch['COD_ID'][0] = b''

        def run(chunk):
            class Layer:
                def GetFeatureCount(self):
                    return n

                def GetArrowStreamAsNumPy(self, options):
                    for start in range(0, n, chunk):
                        yield {k: v[start:start + chunk] for k, v in batch.items()}

            with patch.object(demand, '_open_arrow_layer', return_value=(None, Layer(), set(batch))):
                return demand.read_year_stratified(None, 2025, {}, margin_error=.05)

        first = run(n)
        second = run(777)
        normalize = lambda result: sorted(result[0], key=lambda row: row['COD_ID'])
        self.assertEqual(normalize(first), normalize(second))
        scores = demand._random_scores(np.arange(n, dtype=np.uint64), 2025)
        threshold = int(demand.sample_size(n, margin_error=.05) / n * ((1 << 64) - 1))
        expected = set()
        for group in np.unique(batch['GRU_TAR']):
            indices = np.flatnonzero((batch['GRU_TAR'] == group) & (batch['COD_ID'] != b''))
            expected.update(indices[scores[indices] <= threshold].tolist())
            expected.update(indices[np.argsort(scores[indices])[:demand.MIN_PER_STRATUM]].tolist())
        self.assertEqual({int(r['COD_ID']) for r in first[0]}, expected)
        self.assertAlmostEqual(sum(r['peso_amostral'] for r in first[0]), n - 1, places=4)
        self.assertEqual(first[3]['excluded_empty_id'], 1)
        self.assertEqual(first[3]['covered_strata'], 23)
        self.assertLessEqual(first[3]['kish_effective_n'], len(first[0]) + 1e-8)
        self.assertTrue(all(s['amostra'] >= 10 for s in first[2]))


if __name__ == '__main__':
    unittest.main()
