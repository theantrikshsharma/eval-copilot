"""
Gradio Frontend for Eval Co-pilot.
Provides a clear 4-section interface:
1. Test Cases (View, Filter, Create Golden Pairs)
2. Run Evaluation / Failure Inspector (Execute, Rerun, Inspect Failures & Evidence)
3. Run Comparison (Side-by-side run comparison, failure resolution, new failures)
4. Summary (Persisted execution metrics and top failing checks)
"""

import time
import pandas as pd
import gradio as gr
import db
from adapters import invoke_target
from evaluator import evaluate_response


def get_pair_choices(target_filter="All"):
    pairs = db.get_golden_pairs(target_app=None if target_filter == "All" else target_filter)
    choices = [f"{p['pair_id']} - [{p['target_app']}] {p['input'][:45]}..." if len(p['input']) > 45 else f"{p['pair_id']} - [{p['target_app']}] {p['input']}" for p in pairs]
    return choices


def get_pair_id_from_choice(choice_str: str) -> str:
    if not choice_str:
        return ""
    return choice_str.split(" - ")[0].strip()


def build_pairs_table(target_filter="All"):
    pairs = db.get_golden_pairs(target_app=None if target_filter == "All" else target_filter)
    data = []
    for p in pairs:
        data.append({
            "Pair ID": p["pair_id"],
            "Target App": p["target_app"],
            "Test Input": p["input"],
            "Expected Behaviour": p["expected_behaviour"]
        })
    return pd.DataFrame(data)


def on_select_pair_details(pair_choice):
    pair_id = get_pair_id_from_choice(pair_choice)
    if not pair_id:
        return "", "", "", ""
    pair = db.get_golden_pair(pair_id)
    if not pair:
        return "", "", "", ""
    return (
        pair["pair_id"],
        pair["target_app"],
        pair["input"],
        pair["expected_behaviour"]
    )


def run_single_evaluation(pair_choice, run_id_input):
    pair_id = get_pair_id_from_choice(pair_choice)
    if not pair_id:
        return (
            "ERROR",
            "Please select a valid golden pair first.",
            "No test selected.",
            "",
            pd.DataFrame(columns=["Check Name", "Status", "Evidence or Reason"])
        )

    pair = db.get_golden_pair(pair_id)
    if not pair:
        return (
            "ERROR",
            f"Test case '{pair_id}' not found.",
            "Invalid test ID.",
            "",
            pd.DataFrame(columns=["Check Name", "Status", "Evidence or Reason"])
        )

    run_id = run_id_input.strip() if run_id_input and run_id_input.strip() else f"run_{time.strftime('%Y%m%d_%H%M%S')}"

    # 1. Invoke Target App
    adapter_resp = invoke_target(pair["target_app"], pair["input"], timeout=20.0)

    # 2. Run Evaluator
    eval_res = evaluate_response(
        pair_id=pair["pair_id"],
        target_app=pair["target_app"],
        test_input=pair["input"],
        expected_behaviour=pair["expected_behaviour"],
        resp=adapter_resp
    )

    # 3. Persist to DB
    checks_dict_list = [c.model_dump() for c in eval_res.checks]
    result_id = db.save_run_result(
        run_id=run_id,
        pair_id=pair["pair_id"],
        actual_response=adapter_resp.raw_response,
        overall_status=eval_res.overall_status,
        checks=checks_dict_list
    )

    # 4. Format Output
    checks_df = pd.DataFrame([
        {
            "Check Name": c.check_name,
            "Status": c.status,
            "Evidence or Reason": c.evidence_or_reason
        }
        for c in eval_res.checks
    ])

    meta_info = f"Result ID: #{result_id}  |  Run ID: {run_id}  |  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  |  Target: {pair['target_app']}"
    why_failed = eval_res.why_failed or "All checks passed successfully."

    return (
        eval_res.overall_status,
        why_failed,
        adapter_resp.raw_response or "(No response body)",
        meta_info,
        checks_df
    )


def create_pair(pair_id, target_app, input_text, expected_behaviour):
    if not pair_id or not target_app or not expected_behaviour:
        return "Error: Pair ID, Target App, and Expected Behaviour cannot be blank.", build_pairs_table()
    db.save_golden_pair(pair_id, target_app, input_text, expected_behaviour)
    return f"Successfully saved golden pair '{pair_id}'!", build_pairs_table()


def get_comparison_runs(pair_choice):
    pair_id = get_pair_id_from_choice(pair_choice)
    if not pair_id:
        return gr.Dropdown(choices=[]), gr.Dropdown(choices=[])
    results = db.get_run_results(pair_id=pair_id)
    choices = [f"#{r['result_id']} | [{r['overall_status']}] | Run: {r['run_id']} | {r['timestamp'][:19]}" for r in results]
    if len(choices) >= 2:
        return gr.Dropdown(choices=choices, value=choices[1]), gr.Dropdown(choices=choices, value=choices[0])
    elif len(choices) == 1:
        return gr.Dropdown(choices=choices, value=choices[0]), gr.Dropdown(choices=choices, value=choices[0])
    else:
        return gr.Dropdown(choices=[]), gr.Dropdown(choices=[])


def compare_two_results(run_a_choice, run_b_choice):
    if not run_a_choice or not run_b_choice:
        return (
            "Please select both Execution A and Execution B to compare.",
            "", "", "", "", pd.DataFrame()
        )

    try:
        id_a = int(run_a_choice.split(" | ")[0].replace("#", ""))
        id_b = int(run_b_choice.split(" | ")[0].replace("#", ""))
    except Exception:
        return "Invalid execution selections.", "", "", "", "", pd.DataFrame()

    run_a = db.get_run_result_detail(id_a)
    run_b = db.get_run_result_detail(id_b)

    if not run_a or not run_b:
        return "One or both execution records could not be found.", "", "", "", "", pd.DataFrame()

    checks_a = {c["check_name"]: c for c in run_a.get("checks", [])}
    checks_b = {c["check_name"]: c for c in run_b.get("checks", [])}
    all_check_names = list(dict.fromkeys(list(checks_a.keys()) + list(checks_b.keys())))

    resolved = []
    remaining = []
    new_failures = []
    rows = []

    for name in all_check_names:
        ca = checks_a.get(name)
        cb = checks_b.get(name)

        sa = ca["status"] if ca else "NOT RUN"
        sb = cb["status"] if cb else "NOT RUN"

        ea = ca.get("evidence_or_reason", "Not run in this execution") if ca else "Not run"
        eb = cb.get("evidence_or_reason", "Not run in this execution") if cb else "Not run"

        rows.append({
            "Check Name": name,
            "Run A Status": sa,
            "Run B Status": sb,
            "Run A Evidence": ea,
            "Run B Evidence": eb
        })

        if sa in ("FAIL", "ERROR") and sb == "PASS":
            resolved.append(name)
        elif sa in ("FAIL", "ERROR") and sb in ("FAIL", "ERROR"):
            remaining.append(name)
        elif sa == "PASS" and sb in ("FAIL", "ERROR"):
            new_failures.append(name)

    transition_text = f"**Status Transition:** {run_a['overall_status']} ➔ {run_b['overall_status']}"
    resolved_text = f"**Failures Resolved:** {', '.join(resolved) if resolved else 'None'}"
    remaining_text = f"**Failures Remaining:** {', '.join(remaining) if remaining else 'None'}"
    new_failures_text = f"**Newly Introduced Failures:** {', '.join(new_failures) if new_failures else 'None'}"

    meta_a = f"### Execution A (#{run_a['result_id']})\n- **Status:** {run_a['overall_status']}\n- **Run ID:** {run_a['run_id']}\n- **Time:** {run_a['timestamp']}\n\n**Actual Response:**\n```\n{run_a['actual_response'][:1000]}\n```"
    meta_b = f"### Execution B (#{run_b['result_id']})\n- **Status:** {run_b['overall_status']}\n- **Run ID:** {run_b['run_id']}\n- **Time:** {run_b['timestamp']}\n\n**Actual Response:**\n```\n{run_b['actual_response'][:1000]}\n```"

    delta_summary = f"{transition_text}\n\n- {resolved_text}\n- {remaining_text}\n- {new_failures_text}"

    return (
        delta_summary,
        meta_a,
        meta_b,
        resolved_text,
        remaining_text,
        pd.DataFrame(rows)
    )


def load_summary():
    stats = db.get_summary_stats()
    db_info = db.get_backend_status()

    metrics_md = f"""
### 📊 Execution Metrics (Persisted Records)
| Metric | Count |
|---|---|
| **Total Executions** | **{stats['total_executions']}** |
| **PASS** | 🟢 **{stats['pass_count']}** |
| **FAIL** | 🔴 **{stats['fail_count']}** |
| **ERROR** | 🟠 **{stats['error_count']}** |
| **NEEDS REVIEW** | 🟡 **{stats['needs_review_count']}** |

> **Storage Backend:** `{db_info['backend']}`  
> **Status:** {db_info['note']}
"""
    top_checks_df = pd.DataFrame(stats["top_failing_checks"])
    if top_checks_df.empty:
        top_checks_df = pd.DataFrame(columns=["check_name", "fail_count"])
    else:
        top_checks_df.columns = ["Check Name", "Failure Count"]

    return metrics_md, top_checks_df


def create_gradio_app():
    with gr.Blocks(title="Eval Co-pilot — Checkpoint 4 MVP") as demo:
        gr.Markdown("# 🚀 Eval Co-pilot — Checkpoint 4 MVP")
        gr.Markdown(
            "Evaluation co-pilot for testing live AI applications against golden test cases, "
            "persisting runs, inspecting failures, and comparing reruns without overwriting history."
        )

        with gr.Tabs():
            # ----------------------------------------------------
            # Section A: TEST CASES
            # ----------------------------------------------------
            with gr.TabItem("📋 Section A: Test Cases"):
                gr.Markdown("### Manage & Select Golden Test Pairs")
                with gr.Row():
                    target_filter = gr.Dropdown(
                        choices=["All", "Anupma", "Prateek"],
                        value="All",
                        label="Filter by Target Application"
                    )
                    refresh_pairs_btn = gr.Button("🔄 Refresh List")

                pairs_df = gr.DataFrame(
                    value=build_pairs_table(),
                    headers=["Pair ID", "Target App", "Test Input", "Expected Behaviour"],
                    interactive=False,
                    wrap=True
                )

                gr.Markdown("---")
                gr.Markdown("#### Create & Save a New Golden Pair")
                with gr.Row():
                    new_id = gr.Textbox(label="Pair ID (e.g. A-05, P-05)", placeholder="A-05")
                    new_app = gr.Dropdown(choices=["Anupma", "Prateek"], label="Target Application")
                with gr.Row():
                    new_input = gr.Textbox(label="Test Input", placeholder="What the user sends to the target app...", lines=2)
                    new_expected = gr.Textbox(label="Expected Behaviour", placeholder="What good looks like / explicit constraints...", lines=2)
                create_btn = gr.Button("💾 Save Golden Pair", variant="primary")
                create_status = gr.Markdown("")

                create_btn.click(
                    fn=create_pair,
                    inputs=[new_id, new_app, new_input, new_expected],
                    outputs=[create_status, pairs_df]
                )
                refresh_pairs_btn.click(
                    fn=build_pairs_table,
                    inputs=[target_filter],
                    outputs=[pairs_df]
                )
                target_filter.change(
                    fn=build_pairs_table,
                    inputs=[target_filter],
                    outputs=[pairs_df]
                )

            # ----------------------------------------------------
            # Section B: RUN EVALUATION / FAILURE INSPECTOR
            # ----------------------------------------------------
            with gr.TabItem("🔬 Section B: Run Evaluation / Failure Inspector"):
                gr.Markdown("### Execute Test Case & Inspect Failures")
                with gr.Row():
                    selected_pair = gr.Dropdown(
                        choices=get_pair_choices(),
                        value=get_pair_choices()[0] if get_pair_choices() else None,
                        label="Select Golden Test Case"
                    )
                    refresh_choices_btn = gr.Button("🔄 Reload Cases", size="sm")
                    run_id_box = gr.Textbox(label="Logical Run ID (Optional)", placeholder="e.g. run_batch_1")

                with gr.Row():
                    pair_id_disp = gr.Textbox(label="Test Case ID", interactive=False)
                    target_app_disp = gr.Textbox(label="Target App", interactive=False)
                with gr.Row():
                    input_disp = gr.Textbox(label="Input Sent", lines=2, interactive=False)
                    expected_disp = gr.Textbox(label="Expected Behaviour", lines=2, interactive=False)

                with gr.Row():
                    run_eval_btn = gr.Button("⚡ Execute Test", variant="primary", size="lg")
                    rerun_btn = gr.Button("🔁 Run Just This One Again", variant="secondary", size="lg")

                gr.Markdown("---")
                with gr.Row():
                    overall_status_disp = gr.Textbox(label="Overall Status (PASS / FAIL / ERROR / NEEDS REVIEW)", interactive=False)
                    meta_disp = gr.Textbox(label="Execution Metadata", interactive=False)

                why_failed_disp = gr.Markdown("### Why did it fail?\n*(Run evaluation to see results)*")
                actual_resp_disp = gr.Textbox(label="Exact Actual Response Received", lines=6, interactive=False)

                gr.Markdown("#### Individual Check Results & Evidence")
                checks_table = gr.DataFrame(
                    headers=["Check Name", "Status", "Evidence or Reason"],
                    interactive=False,
                    wrap=True
                )

                # Event handlers
                selected_pair.change(
                    fn=on_select_pair_details,
                    inputs=[selected_pair],
                    outputs=[pair_id_disp, target_app_disp, input_disp, expected_disp]
                )
                refresh_choices_btn.click(
                    fn=lambda: gr.Dropdown(choices=get_pair_choices()),
                    inputs=[],
                    outputs=[selected_pair]
                )

                run_eval_btn.click(
                    fn=run_single_evaluation,
                    inputs=[selected_pair, run_id_box],
                    outputs=[overall_status_disp, why_failed_disp, actual_resp_disp, meta_disp, checks_table]
                )
                rerun_btn.click(
                    fn=run_single_evaluation,
                    inputs=[selected_pair, run_id_box],
                    outputs=[overall_status_disp, why_failed_disp, actual_resp_disp, meta_disp, checks_table]
                )

            # ----------------------------------------------------
            # Section C: RUN COMPARISON
            # ----------------------------------------------------
            with gr.TabItem("⚖️ Section C: Run Comparison"):
                gr.Markdown("### Compare Two Saved Executions of the Same Golden Pair")
                with gr.Row():
                    comp_pair_select = gr.Dropdown(
                        choices=get_pair_choices(),
                        value=get_pair_choices()[0] if get_pair_choices() else None,
                        label="Select Golden Pair for Comparison"
                    )
                    load_runs_btn = gr.Button("🔄 Load Saved Executions")

                with gr.Row():
                    run_a_select = gr.Dropdown(choices=[], label="Execution A (Earlier Run)")
                    run_b_select = gr.Dropdown(choices=[], label="Execution B (Later / Rerun)")

                compare_btn = gr.Button("🔍 Compare Executions Side-by-Side", variant="primary")

                delta_summary_disp = gr.Markdown("### Comparison Summary\nSelect two executions to compare.")

                with gr.Row():
                    exec_a_md = gr.Markdown("### Execution A Details")
                    exec_b_md = gr.Markdown("### Execution B Details")

                gr.Markdown("#### Individual Check Status Changes & Evidence")
                comp_table = gr.DataFrame(
                    headers=["Check Name", "Run A Status", "Run B Status", "Run A Evidence", "Run B Evidence"],
                    interactive=False,
                    wrap=True
                )

                comp_pair_select.change(
                    fn=get_comparison_runs,
                    inputs=[comp_pair_select],
                    outputs=[run_a_select, run_b_select]
                )
                load_runs_btn.click(
                    fn=get_comparison_runs,
                    inputs=[comp_pair_select],
                    outputs=[run_a_select, run_b_select]
                )
                compare_btn.click(
                    fn=compare_two_results,
                    inputs=[run_a_select, run_b_select],
                    outputs=[delta_summary_disp, exec_a_md, exec_b_md, gr.Markdown(visible=False), gr.Markdown(visible=False), comp_table]
                )

            # ----------------------------------------------------
            # Section D: SUMMARY
            # ----------------------------------------------------
            with gr.TabItem("📊 Section D: Summary"):
                gr.Markdown("### Overall Execution Metrics from Persisted Records")
                refresh_summary_btn = gr.Button("🔄 Refresh Summary", variant="secondary")
                summary_metrics_md = gr.Markdown()
                gr.Markdown("#### Most Frequently Failing Checks")
                top_checks_table = gr.DataFrame(
                    headers=["Check Name", "Failure Count"],
                    interactive=False
                )

                refresh_summary_btn.click(
                    fn=load_summary,
                    inputs=[],
                    outputs=[summary_metrics_md, top_checks_table]
                )
                demo.load(
                    fn=load_summary,
                    inputs=[],
                    outputs=[summary_metrics_md, top_checks_table]
                )

    return demo
