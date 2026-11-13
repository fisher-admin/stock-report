import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / 'scripts'
LIB_PATH = SCRIPTS_DIR / 'greenfield_multifactor_lib.py'
BACKTEST_PATH = SCRIPTS_DIR / 'run_greenfield_multifactor_backtest.py'


def load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_lib_module():
    return load_module('greenfield_multifactor_lib', LIB_PATH)


def load_backtest_module():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    load_lib_module()
    return load_module('run_greenfield_multifactor_backtest', BACKTEST_PATH)


class GreenfieldMaterializationOverlayTests(unittest.TestCase):
    def test_select_ranked_holdings_applies_gap_filter_and_industry_cap(self):
        module = load_lib_module()
        overlay = module.MaterializationSelectionOverlay(
            source_path='/tmp/candidate_specs.json',
            candidate_id='D2_sector_heat_alignment',
            top_n=4,
            max_names_per_industry=2,
            min_listing_days=120,
            gap_risk_filter_factor_id='g_gap_risk_filter',
            gap_risk_filter_operator='>=',
            gap_risk_filter_min_zscore=-0.1,
        )
        day = pd.DataFrame(
            {
                'ts_code': ['000001.SZ', '000002.SZ', '000003.SZ', '000004.SZ', '000005.SZ', '000006.SZ'],
                'score': [10.0, 9.0, 8.0, 7.0, 6.0, 5.0],
                'next_return_close_to_close_1d': [0.04, 0.03, 0.02, 0.01, -0.01, 0.05],
                'industry': ['专用机械', '专用机械', '电气设备', '电气设备', '软件服务', '软件服务'],
                'listing_days': [200, 200, 130, 130, 200, 90],
                'g_gap_risk_filter': [0.2, -0.3, 0.1, 0.0, -0.2, 0.4],
            }
        )

        chosen = module.select_ranked_holdings(
            day,
            score_col='score',
            return_col='next_return_close_to_close_1d',
            top_n=module.effective_top_n(20, overlay),
            selection_overlay=overlay,
        )

        self.assertEqual(list(chosen['ts_code']), ['000001.SZ', '000003.SZ', '000004.SZ'])
        self.assertTrue((chosen['g_gap_risk_filter'] >= -0.1).all())
        self.assertLessEqual(chosen['industry'].value_counts().max(), 2)
        self.assertTrue((chosen['listing_days'] >= 120).all())

    def test_evaluate_portfolio_uses_overlay_top_n_override(self):
        module = load_lib_module()
        overlay = module.MaterializationSelectionOverlay(
            source_path='/tmp/candidate_specs.json',
            candidate_id='D2_sector_heat_alignment',
            top_n=1,
            gap_risk_filter_factor_id='g_gap_risk_filter',
            gap_risk_filter_operator='>=',
            gap_risk_filter_min_zscore=-0.1,
        )
        panel = pd.DataFrame(
            {
                'trade_date': pd.to_datetime(['2026-04-01', '2026-04-01', '2026-04-02', '2026-04-02']),
                'ts_code': ['A', 'B', 'A', 'B'],
                'score': [10.0, 9.0, 8.0, 7.0],
                'next_return_close_to_close_1d': [0.05, 0.01, 0.03, -0.02],
                'g_gap_risk_filter': [0.2, -0.5, 0.1, -0.4],
            }
        )

        result = module.evaluate_portfolio(
            panel,
            score_col='score',
            return_col='next_return_close_to_close_1d',
            top_n=3,
            round_trip_cost=0.0,
            min_positions=1,
            selection_overlay=overlay,
        )

        self.assertEqual(list(result['daily']['net_return'].round(6)), [0.05, 0.03])
        self.assertEqual(list(result['daily']['holding_count']), [1, 1])
        self.assertEqual(result['metrics']['days'], 2)

    def test_resolve_materialization_overlay_reads_locked_candidate_risk_overlay(self):
        lib_module = load_lib_module()
        backtest_module = load_backtest_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            candidate_specs = tmp_path / 'candidate_specs.json'
            candidate_specs.write_text(
                json.dumps(
                    {
                        'seed_candidate_ranked': [
                            {
                                'candidate_id': 'D2_sector_heat_alignment',
                                'weights': {'g_sector_heat_alignment': 0.6, 'g_liquid_leadership': 0.4},
                            }
                        ],
                        'candidate_specs': [
                            {
                                'candidate_id': 'D2_sector_heat_alignment',
                                'normalized_weights': {'g_sector_heat_alignment': 0.6, 'g_liquid_leadership': 0.4},
                                'selected_factors': [
                                    {
                                        'factor_id': 'g_sector_heat_alignment',
                                        'family': 'sector_rotation',
                                        'direction': 'long',
                                    },
                                    {
                                        'factor_id': 'g_liquid_leadership',
                                        'family': 'liquid_leadership',
                                        'direction': 'long',
                                    },
                                ],
                                'risk_overlay': {
                                    'gap_risk_filter': {
                                        'factor_id': 'g_gap_risk_filter',
                                        'operator': '>=',
                                        'min_zscore': -0.1,
                                    },
                                    'portfolio_shape': {
                                        'top_n': 20,
                                        'max_names_per_industry': 2,
                                        'min_listing_days': 120,
                                    },
                                },
                            }
                        ],
                        'candidate_pool': [
                            {'factor_id': 'g_sector_heat_alignment', 'family': 'sector_rotation', 'direction': 'long'},
                            {'factor_id': 'g_liquid_leadership', 'family': 'liquid_leadership', 'direction': 'long'},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding='utf-8',
            )

            cfg = lib_module.GreenfieldConfig(
                root=tmp_path,
                workspace=tmp_path,
                stock_data_root=tmp_path,
                output_root=tmp_path / 'outputs',
                panel_path=tmp_path / 'panel.parquet',
                industry_map_path=tmp_path / 'industry.parquet',
                pit_financial_dir=tmp_path / 'pit',
                current_market_fingerprint_path=tmp_path / 'fingerprint.json',
                regime_analog_windows_path=tmp_path / 'analog.json',
                market_state_path=tmp_path / 'state.json',
                market_snapshot_path=tmp_path / 'snapshot.json',
                market_morning_brief_path=tmp_path / 'brief.json',
                start_date='2021-01-01',
                end_date=None,
                top_n=20,
                round_trip_cost=0.002,
                min_listing_days=120,
                train_days=252,
                validation_days=63,
                test_days=63,
                step_days=63,
                min_train_days=160,
                analog_max_windows=10,
                analog_lookback_days=252,
                analog_min_lookback_days=160,
                family_budgets={},
                factor_registry={'backtestable_factors': [], 'current_only_factors': []},
                scoring_controls={},
                selection_controls={},
                materialization_controls={
                    'locked_weight_source': str(candidate_specs),
                    'locked_candidate_id': 'D2_sector_heat_alignment',
                    'apply_declared_overlay': True,
                },
                regime_controls={},
                scenario_meta={},
            )

            locked_candidate = backtest_module._load_locked_candidate(cfg)
            overlay, context = backtest_module._resolve_materialization_selection_overlay(cfg)

            self.assertEqual(locked_candidate['weights'], {'g_sector_heat_alignment': 0.6, 'g_liquid_leadership': 0.4})
            self.assertEqual(locked_candidate['context']['selected_factor_count'], 2)
            self.assertEqual(locked_candidate['context']['selected_family_count'], 2)
            self.assertEqual(context['status'], 'declared_overlay_enforced')
            self.assertIsNotNone(overlay)
            self.assertEqual(overlay.candidate_id, 'D2_sector_heat_alignment')
            self.assertEqual(overlay.max_names_per_industry, 2)
            self.assertEqual(overlay.min_listing_days, 120)
            self.assertEqual(overlay.gap_risk_filter_factor_id, 'g_gap_risk_filter')
            self.assertEqual(overlay.gap_risk_filter_min_zscore, -0.1)


if __name__ == '__main__':
    unittest.main()
