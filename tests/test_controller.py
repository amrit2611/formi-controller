"""Unit tests for the parts where a bug would silently corrupt decisions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))

from main import config_diff, step_toward, tv_distance  # noqa: E402
from stats import min_sample, z_critical, z_score  # noqa: E402
from store import Store  # noqa: E402


def test_z_score_matches_hand_calculation():
    assert round(z_score(1131, 2262, 629, 1154), 2) == 2.49


def test_bonferroni_line_for_three_comparisons():
    assert round(z_critical(0.05, 3), 2) == 2.39


def test_min_sample_rule_of_thumb():
    assert min_sample(0.5, 0.06) == 1112


def test_chunk_and_cursor_move_together(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    key = ("wf1", "2027-01-04", "voice", "V1")
    s.save_chunk(0, {key: [10, 3, 1, 0]}, [])
    s.save_chunk(1, {key: [10, 2, 2, 0]}, [])
    assert s.last_chunk() == 1
    assert s.totals("wf1", "voice", "0000")["V1"] == {"dials": 20, "connected": 5, "engaged": 3, "hallucinated": 0}


def test_config_diff_only_shows_what_changed():
    old = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 1}}}, "prompt_version": "P1", "schedule": {}}
    new = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 0.4, "V2": 0.6}}}, "prompt_version": "P1", "schedule": {}}
    assert config_diff(old, new) == {"allocations.default.voice": {"V1": 0.4, "V2": 0.6}}


def test_step_toward_never_exceeds_max_shift():
    old, target = {"D1": 0.5, "D2": 0.5}, {"D2": 0.5, "D3": 0.5}
    step = step_toward(old, target, 0.2)
    assert tv_distance(old, step) <= 0.2
