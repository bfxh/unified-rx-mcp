"""unified-rx 引擎层——合并后的大模块 + 兼容 re-export。
新技术 = 往对应 engine 增量加函数（不新建零散文件）。
旧模块名 import 继续可用（兼容层）。"""
from engine import scan_engine, infra_engine, ide_engine, learn_engine  # noqa: F401,E402

# ── ide_engine（合并自：ide_tools, ide_ui, ide_session, ide_commands, ide_cache, ide_fusion, rx_ide）
from engine.ide_engine import ide_actions, ide_complete, ide_references, ide_rename
from engine.ide_engine import AboutWin, CodeEditor, FileTree, IdeApp, LogPanel, ScanPanel, StatsPanel, TelemetryWin, ToolPanel, main
from engine.ide_engine import FastLineIndex, PieceTable, _Piece
from engine.ide_engine import cheatsheet, local_run
from engine.ide_engine import cached, enable_persistence, file_version, invalidate, is_cached, stats, store
from engine.ide_engine import annotate_issues, cross_validate_impact, impact_via_references, record_ide_usage
from engine.ide_engine import main

# ── learn_engine（合并自：patch_learn, differentiable_code, explore_engine, distill_pipeline, quality_engine, failure_analyze, mini_bert_tokenizer, replay_core）
from engine.learn_engine import patch_learn
from engine.learn_engine import check_perf_target, embed_function, optimize_code, similar_functions
from engine.learn_engine import ExploreEngine, _Node, normalize_goals
from engine.learn_engine import distill, export_onnx, main, prepare_data
from engine.learn_engine import QualityEngine
from engine.learn_engine import failure_analyze
from engine.learn_engine import MiniBertTokenizer, encode_batch
from engine.learn_engine import replay_list, replay_record, replay_run, replays_dir

# ── scan_engine（合并自：bug_scan_core, std_core, ui_check_core, cov_scan, cross_taint, rust_scan, sage_scan）
from engine.scan_engine import ext_rules_scan, load_ext_rules, py_taint_scan
from engine.scan_engine import scan_directory, scan_file
from engine.scan_engine import scan_ui_dir, scan_ui_source
from engine.scan_engine import cov_scan
from engine.scan_engine import cross_taint_scan
from engine.scan_engine import scan_rust_file
from engine.scan_engine import sage_scan
