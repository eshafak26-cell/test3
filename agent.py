import contextlib
import copy
import io
import json
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from prepare import prepare_input
from run import make_cmd_file, names, prepare_settings, read_config
from tools import read_input, read_json, write_input


AI_DIR = Path("outputs/ai")
PREPAY_NAMES = {1.0: "unch", 1.5: "Prepayx1.5", 2.0: "Prepayx2"}
RATE_NAMES = {}
for shift_bp in (-100, -75, -50, -25, 0, 25, 50, 75, 100):
    RATE_NAMES[shift_bp] = "unch" if shift_bp == 0 else f"Rate{shift_bp:+d}"
METRICS = ["Price", "OAS", "OA Dur", "OA CPR", "LT. CPR", "DV01"]
METRIC_DECIMALS = {"Price": 2, "OAS": 0, "OA Dur": 1, "OA CPR": 1, "LT. CPR": 1}


def format_metric(metric, value):
    decimals = METRIC_DECIMALS.get(metric, 4 if abs(value) < 1000 else 2)
    return f"{value:,.{decimals}f}"


def results_table(results):
    comparison = results["comparison"]
    groups = {}
    for result in comparison or results["metrics"]:
        key = (result["row"], result["cusip"], result["scenario"])
        groups.setdefault(key, []).append(result)
    lines = []
    for (row, cusip, scenario), values in groups.items():
        lines.extend(["**" + cusip + " · Input row " + str(row + 1) + " · " + scenario + "**", ""])
        if comparison:
            lines.extend(["| Metric | Base | " + scenario + " | Difference |", "|---|---:|---:|---:|"])
        else:
            lines.extend(["| Metric | Value |", "|---|---:|"])
        for result in values:
            metric = result["metric"]
            cells = [metric]
            if comparison:
                cells.append(format_metric(metric, result["base"]))
            cells.append(format_metric(metric, result["value"]))
            if comparison:
                cells.append(format_metric(metric, result["difference"]))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def save_json(path, content):
    with path.open("w", encoding="utf-8") as json_file:
        json.dump(content, json_file, ensure_ascii=False, indent=4)


def analysis_file(analysis_id):
    analysis_files = {}
    for path in AI_DIR.glob("analysis_*/analysis.json"):
        analysis_files[path.parent.name] = path
    return analysis_files[analysis_id]


def job_file(job_id):
    job_files = {}
    for path in AI_DIR.glob("analysis_*/job_*/job.json"):
        job_files[path.parent.name] = path
    return job_files[job_id]


def read_analysis(analysis_id):
    return read_json(analysis_file(analysis_id))


def read_job(job_id):
    return read_json(job_file(job_id))


def select_asset(input_file, cusip):
    input_rows = read_input(input_file)
    selected_rows = input_rows.loc[input_rows["Cusip"] == cusip].copy()
    return selected_rows


def list_assets(pricing_date):
    config = read_config("config.json", "base", {
        "pricing_date": pricing_date, "positions": "TBA", "run_batchcal": False
    })
    settings = prepare_settings(config)
    input_rows = read_input(settings["input_file"])
    assets = []
    for cusip in input_rows["Cusip"].drop_duplicates():
        selected_rows = input_rows.loc[input_rows["Cusip"] == cusip]
        assets.append({"cusip": cusip, "input_rows": len(selected_rows)})
    return assets


def prepare_base(cusip, pricing_date):
    analysis_id = "analysis_" + uuid.uuid4().hex[:12]
    analysis_dir = AI_DIR / analysis_id
    input_dir = analysis_dir / "inputs"
    input_dir.mkdir(parents=True)
    override = {
        "pricing_date": pricing_date, "positions": "TBA",
        "run_batchcal": False, "run_name": analysis_id
    }
    config = read_config("config.json", "base", override)
    base_settings = prepare_settings(config)
    selected_rows = select_asset(base_settings["input_file"], cusip)
    selected_input = write_input(selected_rows, str(input_dir / "selected_asset.csv"))
    base_settings["input_file"] = selected_input
    base_settings = prepare_input(base_settings, names)
    config = read_config("config.json", "spot", override)
    spot_settings = prepare_settings(config)
    spot_settings["input_file"] = selected_input
    spot_settings["mr"] = base_settings["mr"]
    scn_text = Path("config/scn/oas_shock.scn").read_text(encoding="utf-8")
    zero_scen = re.search(r'<Scenario Name="unch">.*?</Scenario>', scn_text, re.S).group(0)
    analysis = {
        "analysis_id": analysis_id, "cusip": cusip,
        "pricing_date": pricing_date, "created_at": datetime.now().isoformat(),
        "input_rows": len(selected_rows), "input_file": selected_input,
        "base_settings": base_settings, "spot_settings": spot_settings,
        "zero_scen": zero_scen
    }
    save_json(analysis_dir / "analysis.json", analysis)
    job = prepare_job(analysis_id, "base", {}, base_settings, [])
    analysis["base_job_id"] = job["job_id"]
    save_json(analysis_dir / "analysis.json", analysis)
    return job


def prepare_prepay(analysis_id, prepay_factor):
    return prepare_scen_job(
        analysis_id, "prepay", {"prepay_factor": prepay_factor},
        [], {"Total Smm Asset Multiplier": prepay_factor}
    )


def prepare_rate_shift(analysis_id, shift_bp):
    scen_name = RATE_NAMES[shift_bp]
    scens = [{"name": "unch", "shift_bp": 0}]
    if shift_bp != 0:
        scens.append({"name": scen_name, "shift_bp": shift_bp})
    return prepare_scen_job(
        analysis_id, "rate_shift", {"shift_bp": shift_bp}, scens, {}
    )


def prepare_curve(analysis_id):
    scens = []
    for shift_bp, scen_name in RATE_NAMES.items():
        scens.append({"name": scen_name, "shift_bp": shift_bp})
    return prepare_scen_job(
        analysis_id, "curve", {"shift_bp": list(RATE_NAMES)}, scens, {}
    )


def prepare_scen_job(analysis_id, operation, parameters, scens, setfields):
    analysis = read_analysis(analysis_id)
    base_status = read_job_status(analysis["base_job_id"])
    if base_status["status"] != "completed":
        next_step = {
            "prepared": "Execute the prepared Base job first, then prepare the requested scenario again after it completes.",
            "running": "Wait for the Base job to finish and check read_job_status. Do not execute it again.",
            "failed": "Report the Base failure and stop. Do not execute this failed job again. Create a new Base only if the user requests it.",
        }[base_status["status"]]
        return {
            "analysis_id": analysis_id, "operation": operation, "parameters": parameters,
            "created": False,
            "reason": "The " + operation + " job was not created because the Base job "
                      + base_status["job_id"] + " is " + base_status["status"] + ". " + next_step,
            "base_job_id": base_status["job_id"], "base_status": base_status["status"],
            "base_error": base_status["error"]
        }
    settings = copy.deepcopy(
        analysis["spot_settings"] if scens else analysis["base_settings"]
    )
    settings.update(setfields)
    return prepare_job(analysis_id, operation, parameters, settings, scens)


def write_scn_file(zero_scen, scens, scn_file):
    scn_lines = [str(len(scens))]
    for scen_values in scens:
        scen = zero_scen.replace('Name="unch"', 'Name="' + scen_values["name"] + '"', 1)
        if "PrepayMult" in scen_values:
            scen = re.sub(
                r"(<PrepayMult>)[^<]*(</PrepayMult>)",
                lambda match: match[1] + str(scen_values["PrepayMult"]) + match[2],
                scen, count=1
            )
        if "shift_bp" in scen_values:
            for curve_name in ("GovtShift", "LiborShift", "AgencyShift"):
                pattern = r"(<" + curve_name + r"\b[^>]*>\s*<TS\b[^>]*\bRate=)(['\"])[^'\"]*\2"
                scen = re.sub(
                    pattern,
                    lambda match: match[1] + match[2] + str(float(scen_values["shift_bp"])) + match[2],
                    scen, count=1
                )
        scn_lines.extend(["Version 20", scen, ""])
    scn_file.write_text("\n".join(scn_lines), encoding="utf-8", newline="\r\n")


def prepare_job(analysis_id, operation, parameters, base_settings, scens):
    analysis = read_analysis(analysis_id)
    job_id = "job_" + uuid.uuid4().hex[:12]
    job_dir = analysis_file(analysis_id).parent / job_id
    job_dir.mkdir()
    settings = copy.deepcopy(base_settings)
    settings["output_dir"] = str(job_dir)
    settings["output_file"] = str(job_dir / "results.pf")
    settings["cmd_file"] = str(job_dir / "commands.cmd")
    settings["scenarios_folder"] = str(job_dir)
    for path_name in ("PPBDG_OUTPUT_PATH", "PPBDG_LOG_PATH"):
        if settings[path_name] != "":
            settings[path_name] = str(job_dir / "logs")
    if settings["DEBUG_FILE"] != "":
        settings["DEBUG_FILE"] = str(job_dir / "logs" / "debug.log")
    template_columns = ["Cusip", "Holding", "Description", "Calc Error"] + METRICS
    settings["template"] = str(job_dir / "template.csv")
    (job_dir / "template.csv").write_text(
        ",".join(template_columns) + "\n", encoding="utf-8"
    )
    if scens:
        scn_file = job_dir / "scenarios.scn"
        write_scn_file(analysis["zero_scen"], scens, scn_file)
        settings["scenario"]["scn"] = str(scn_file)
        settings["scenario"]["scnrptflds"] = str(job_dir / "scn_fields.txt")
        (job_dir / "scn_fields.txt").write_text(
            "\n".join(METRICS) + "\n", encoding="utf-8"
        )
    with contextlib.redirect_stdout(io.StringIO()):
        run_files = make_cmd_file(copy.deepcopy(settings))
    save_json(job_dir / "settings.json", settings)
    job = {
        "job_id": job_id, "analysis_id": analysis_id, "operation": operation,
        "parameters": parameters, "status": "prepared", "scens": scens,
        "run_files": run_files, "created_at": datetime.now().isoformat()
    }
    save_json(job_dir / "job.json", job)
    return read_job_status(job_id)


def read_job_status(job_id):
    job = read_job(job_id)
    analysis = read_analysis(job["analysis_id"])
    return {
        "job_id": job_id, "analysis_id": job["analysis_id"],
        "cusip": analysis["cusip"], "pricing_date": analysis["pricing_date"],
        "operation": job["operation"], "parameters": job["parameters"],
        "status": job["status"], "error": job.get("error", ""),
        "input_rows": analysis["input_rows"], "runs": 1,
        "scenarios": len(job["scens"])
    }


def read_job_metrics(job):
    job_dir = job_file(job["job_id"]).parent
    metrics = []
    if not job["scens"]:
        scen_name = "Base"
        if job["operation"] == "prepay":
            scen_name = PREPAY_NAMES[job["parameters"]["prepay_factor"]]
        report_file = Path(job["run_files"]["csv_file"])
        if not report_file.is_file():
            return metrics
        result_rows = read_input(report_file)
        if "Calc Error" in result_rows.columns and result_rows["Calc Error"].str.strip().ne("").any():
            job["error"] = "The output contains calculation errors. See the local CSV."
            return metrics
        for row_number, result_row in result_rows.iterrows():
            for metric in METRICS:
                if metric in result_rows.columns and result_row[metric] != "":
                    metrics.append({
                        "row": int(row_number), "cusip": result_row["Cusip"],
                        "scenario": scen_name, "metric": metric,
                        "value": float(result_row[metric])
                    })
    else:
        for metric in METRICS:
            report_file = job_dir / ("results_" + metric + ".csv")
            if report_file.is_file():
                result_rows = pd.read_csv(report_file, dtype=str, keep_default_na=False, encoding="latin1")
                result_rows.columns = result_rows.columns.str.strip()
                if "Calc Error" in result_rows.columns and result_rows["Calc Error"].str.strip().ne("").any():
                    job["error"] = "The output contains calculation errors. See the local CSV."
                    return []
                for row_number, result_row in result_rows.iterrows():
                    for scen in job["scens"]:
                        scen_name = scen["name"]
                        if scen_name in result_rows.columns and result_row[scen_name] != "":
                            metrics.append({
                                "row": int(row_number), "cusip": result_row["Cusip"],
                                "scenario": scen_name, "metric": metric,
                                "value": float(result_row[scen_name])
                            })
    return metrics


def compare_metrics(metrics, zero_name):
    base_values = {}
    for metric in metrics:
        if metric["scenario"] == zero_name:
            base_values[(metric["row"], metric["cusip"], metric["metric"])] = metric["value"]
    comparison = []
    for metric in metrics:
        if metric["scenario"] != zero_name:
            base_value = base_values[(metric["row"], metric["cusip"], metric["metric"])]
            comparison.append({
                **metric, "base": base_value,
                "difference": metric["value"] - base_value
            })
    return comparison


def execute_job(job_id):
    job = read_job(job_id)
    if job["status"] != "prepared":
        return read_job_status(job_id)
    job_dir = job_file(job_id).parent
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat()
    save_json(job_dir / "job.json", job)
    with (job_dir / "run.log").open("w", encoding="utf-8") as run_log:
        process_result = subprocess.run(
            ["cmd", "/d", "/c", job["run_files"]["cmd_file"]],
            stdout=run_log, stderr=subprocess.STDOUT
        )
    job["returncode"] = process_result.returncode
    job["status"] = "failed"
    if process_result.returncode == 0:
        metrics = read_job_metrics(job)
        if job["operation"] == "prepay":
            analysis = read_analysis(job["analysis_id"])
            base_metrics = read_json(job_dir.parent / analysis["base_job_id"] / "metrics.json")
            comparison = compare_metrics(base_metrics + metrics, "Base")
        elif job["scens"]:
            comparison = compare_metrics(metrics, "unch")
        else:
            comparison = []
        if metrics:
            save_json(job_dir / "metrics.json", metrics)
            save_json(job_dir / "comparison.json", comparison)
            if comparison:
                pd.DataFrame(comparison).to_csv(job_dir / "comparison.csv", index=False)
                from charts import draw_comparison
                job["plot_files"] = draw_comparison(job_dir, job, comparison)
            job["status"] = "completed"
        else:
            job["error"] = job.get("error", "No supported metrics were found in this job's output.")
    else:
        job["error"] = "BatchCal did not complete. See the local run.log."
    job["finished_at"] = datetime.now().isoformat()
    save_json(job_dir / "job.json", job)
    return read_job_status(job_id)


def read_job_results(job_id):
    status = read_job_status(job_id)
    if status["status"] != "completed":
        return status
    job_dir = job_file(job_id).parent
    status["metrics"] = read_json(job_dir / "metrics.json")
    status["comparison"] = read_json(job_dir / "comparison.json")
    status["plots"] = []
    for plot_file in read_job(job_id).get("plot_files", []):
        status["plots"].append(Path(plot_file).name)
    status["how_to_read"] = {
        "difference": "the Base job value for prepay jobs; the zero-shift (unch) value computed inside this same job for rate jobs",
        "base": "the separately executed Base job" if status["operation"] in ("base", "prepay") else "the zero-shift scenario of this job, run from the saved analysis settings; it is not the separately executed Base job",
        "scenarios": "each scenario starts from the saved Base settings; shocks do not accumulate across jobs",
        "rows": "one row per input holding; rows of the same Cusip are reported separately, not merged",
        "DV01": "engine field name kept as is; not derived from a duration",
        "prepay_factor": "Total Smm Asset Multiplier applied to the Base settings; not a proportional CPR change",
    }
    return status
