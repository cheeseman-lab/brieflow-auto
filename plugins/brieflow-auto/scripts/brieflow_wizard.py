#!/usr/bin/env python3
"""brieflow_wizard.py — helpers for the interview-driven screen wizard.

The wizard is no longer a CLI. The agent (Claude Code, via the
`skills/brieflow-auto/SKILL.md` skill) drives an interactive interview with the
operator. State lives in a single JSON file per screen:

    <analysis-dir>/.brieflow/interview.json

These helpers are the I/O + gating layer the agent calls. Reasoning
(regex inference, channel-role mapping, auto-probing data dirs) stays in the
agent. See `skills/brieflow-auto/references/wizard_workflow.md` for the
agent-facing protocol.

The marimo gen for each phase reads its filled-in section from
`interview.json` and writes its own block of `config.yml` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Self-bootstrap this directory onto sys.path so sibling modules resolve whether
# the wizard is imported by name (PYTHONPATH / scratchpad sys.path.insert) or
# spec-loaded by file path (the test harness). Mirrors brieflow_runner.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Public taxonomy re-exported from brieflow_buckets. The redundant `X as X`
# form marks these as intentional re-exports (preserves the historical
# `from brieflow_wizard import VALID_VALUE_SOURCES` contract; lint-clean).
from brieflow_buckets import (  # noqa: E402
    AGENT_RESOLVED_BUCKETS as AGENT_RESOLVED_BUCKETS,
    ALL_BUCKETS as ALL_BUCKETS,
    ARCHIVAL_BUCKETS as ARCHIVAL_BUCKETS,
    OPERATOR_BUCKETS as OPERATOR_BUCKETS,
    SYSTEM_DERIVED_BUCKETS as SYSTEM_DERIVED_BUCKETS,
    VALID_VALUE_SOURCES as VALID_VALUE_SOURCES,
    _NO_MARKER_BUCKETS as _NO_MARKER_BUCKETS,
    _OPERATOR_BUCKETS as _OPERATOR_BUCKETS,
    validate_interview_buckets as validate_interview_buckets,
)
from brieflow_resolvers import (  # noqa: E402
    AGENT_INTERVIEW as AGENT_INTERVIEW,
    DERIVATION as DERIVATION,
    KNOWN_UNIMPLEMENTED_PROBES as KNOWN_UNIMPLEMENTED_PROBES,
    RESOLVER_MAP as RESOLVER_MAP,
    resolve_phase as resolve_phase,
    validate_resolver_coverage as validate_resolver_coverage,
)
from brieflow_probe import (  # noqa: E402
    detect_data_format as detect_data_format,
    detect_organization as detect_organization,
    detect_round_order as detect_round_order,
    detect_z_planes as detect_z_planes,
    infer_path_pattern as infer_path_pattern,
    probe_preprocess as probe_preprocess,
    probe_det_range as probe_det_range,
    probe_phenotype_cells_per_tile as probe_phenotype_cells_per_tile,
)
from brieflow_tiles import (  # noqa: E402
    _farthest_point_indices as _farthest_point_indices,
    list_wells_from_metadata as list_wells_from_metadata,
    select_initial_sbs_tiles as select_initial_sbs_tiles,
    select_test_tile as select_test_tile,
    select_test_tile_from_metadata as select_test_tile_from_metadata,
    select_validation_tiles_from_metadata as select_validation_tiles_from_metadata,
    select_tiles_one_per_well as select_tiles_one_per_well,
)
from brieflow_markers import (  # noqa: E402
    find_marker_cell as find_marker_cell,
    patch_marker_block_value as patch_marker_block_value,
    scan_marker_blocks as scan_marker_blocks,
    scan_variable_collisions as scan_variable_collisions,
    verify_marker_coverage as verify_marker_coverage,
)
from brieflow_channels import (  # noqa: E402
    DEFAULT_CMAP_PALETTE as DEFAULT_CMAP_PALETTE,
    default_channel_cmaps as default_channel_cmaps,
)
from brieflow_marimo import (  # noqa: E402
    _await_kernel_settle as _await_kernel_settle,
    setup_kernel_tees as setup_kernel_tees,
    start_marimo_kernel as start_marimo_kernel,
    stop_marimo_kernel as stop_marimo_kernel,
)
from brieflow_sweep import (  # noqa: E402
    _fan_out_notebook_runs as _fan_out_notebook_runs,
    adaptive_marimo_sweep_multitile as adaptive_marimo_sweep_multitile,
    adaptive_sweep_headless as adaptive_sweep_headless,
    joint_marimo_sweep_multitile as joint_marimo_sweep_multitile,
    export_phase_html as export_phase_html,
    parse_qc_block as parse_qc_block,
    parse_resolution_table as parse_resolution_table,
    pick_sweep_winner as pick_sweep_winner,
    cached_sweep_headless as cached_sweep_headless,
    adaptive_cached_sweep as adaptive_cached_sweep,
    sweep_evidence as sweep_evidence,
    refine_points as refine_points,
    run_marimo_sweep as run_marimo_sweep,
    select_final_resolutions as select_final_resolutions,
    cluster_target_miss as cluster_target_miss,
    sweep_headless as sweep_headless,
    validate_across_tiles as validate_across_tiles,
    validate_tiles_headless as validate_tiles_headless,
)
from brieflow_interview import (  # noqa: E402
    _DEFAULT_TEMPLATE as _DEFAULT_TEMPLATE,
    _DEFAULT_LAB_DEFAULTS as _DEFAULT_LAB_DEFAULTS,
    _screen_id as _screen_id,
    init_screen_interview as init_screen_interview,
    _lookup_dotted as _lookup_dotted,
    _compute_barcode_cycle_positions as _compute_barcode_cycle_positions,
    _compute_aggregate_fields as _compute_aggregate_fields,
    _compute_phenotype_cmaps as _compute_phenotype_cmaps,
    _compute_sbs_cmaps as _compute_sbs_cmaps,
    _compute_sbs_segment_cells as _compute_sbs_segment_cells,
    _compute_call_reads_method as _compute_call_reads_method,
    _compute_channels_metadata as _compute_channels_metadata,
    _compute_merge_approach as _compute_merge_approach,
    apply_merge_approach as apply_merge_approach,
    derive_merge_approach as derive_merge_approach,
    fill_lab_defaults as fill_lab_defaults,
    merge_approach_policy as merge_approach_policy,
    objective_magnification as objective_magnification,
    _COMPUTED_DERIVATIONS as _COMPUTED_DERIVATIONS,
    load_interview as load_interview,
    save_interview as save_interview,
    filter_params as filter_params,
    find_param as find_param,
    _suggest_param_alternatives as _suggest_param_alternatives,
    set_value as set_value,
    _GATING_RE as _GATING_RE,
    _GATE_OPS as _GATE_OPS,
    _resolve_gate_row as _resolve_gate_row,
    _gate_satisfied as _gate_satisfied,
    _eval_structured_gate as _eval_structured_gate,
    is_active as is_active,
    tuned_export_path as tuned_export_path,
    pull_tuned_values as pull_tuned_values,
    record_tuned_confirmed as record_tuned_confirmed,
    _CANONICAL_TYPE_FOR as _CANONICAL_TYPE_FOR,
    derive_metadata_order_type as derive_metadata_order_type,
    confirm_channel_mapping as confirm_channel_mapping,
    record_channel_mapping as record_channel_mapping,
    interview_completeness as interview_completeness,
    expected_operator_params as expected_operator_params,
    load_notebook_defaults as load_notebook_defaults,
    resolve_all_phases as resolve_all_phases,
    scan_additional_notes as scan_additional_notes,
    verify_notes_against_data as verify_notes_against_data,
    interview_review as interview_review,
    render_interview_review as render_interview_review,
    render_interview_review_html as render_interview_review_html,
    _load_screen_yaml as _load_screen_yaml,
    apply_preprocess_probe as apply_preprocess_probe,
    _now_iso as _now_iso,
)
from brieflow_notebook_ops import (  # noqa: E402
    audit_canonical_notebooks as audit_canonical_notebooks,
    confirm_tuned_loop as confirm_tuned_loop,
    dry_export_notebook as dry_export_notebook,
    preflight_notebook as preflight_notebook,
    reset_phase as reset_phase,
    verify_phase_outputs as verify_phase_outputs,
)
from brieflow_findings import record_finding as record_finding  # noqa: E402
