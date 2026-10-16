import asyncio
import io
import json
import os
from typing import Annotated, Literal

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

import agent
from run import scen_settings

RESULTS_UI = "ui://polypaths-wrapper/results.html"

apps = Apps()
apps.add_html_resource(
    RESULTS_UI,
    io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_app.html"), encoding="utf-8").read(),
    name="results",
    title="PolyPaths results",
)


@apps.tool(resource_uri=RESULTS_UI, description="Display a completed job's HTML comparison cards or SVG rate curves inline. Call when read_job_results returns nonempty comparison data. Base jobs use a normal table in chat and do not need this tool.")
async def show_job_chart(job_id: str) -> CallToolResult:
    results = agent.read_job_results(job_id)
    chart = {key: results[key] for key in ("job_id", "cusip", "pricing_date", "operation", "parameters", "status")}
    cards = {}
    curves = {}
    if results["operation"] == "curve":
        shifts = {}
        for scenario in agent.read_job(job_id)["scens"]:
            shifts[scenario["name"]] = scenario["shift_bp"]
        for metric in results.get("comparison", []):
            key = (metric["row"], metric["cusip"])
            if key not in curves:
                curves[key] = {"row": metric["row"], "cusip": metric["cusip"], "metrics": {}}
            series = curves[key]["metrics"]
            name = metric["metric"]
            if name not in series:
                series[name] = {
                    "name": name, "decimals": agent.METRIC_DECIMALS.get(name),
                    "points": [{"shift": 0, "value": metric["base"], "label": agent.format_metric(name, metric["base"])}],
                }
            series[name]["points"].append({
                "shift": shifts[metric["scenario"]], "value": metric["value"],
                "label": agent.format_metric(name, metric["value"]),
            })
        for group in curves.values():
            series = []
            for name in agent.METRICS:
                if name in group["metrics"]:
                    metric = group["metrics"][name]
                    metric["points"].sort(key=lambda point: point["shift"])
                    series.append(metric)
            group["metrics"] = series
    else:
        for metric in results.get("comparison", []):
            key = (metric["row"], metric["cusip"], metric["scenario"])
            if key not in cards:
                cards[key] = {"row": metric["row"], "cusip": metric["cusip"], "scenario": metric["scenario"], "metrics": []}
            difference = agent.format_metric(metric["metric"], metric["difference"])
            direction = "flat"
            if float(difference.replace(",", "")) != 0:
                direction = "up" if metric["difference"] > 0 else "down"
            else:
                difference = difference.lstrip("-")
            cards[key]["metrics"].append({
                "name": metric["metric"],
                "base": agent.format_metric(metric["metric"], metric["base"]),
                "value": agent.format_metric(metric["metric"], metric["value"]),
                "difference": ("+" if direction == "up" else "") + difference,
                "direction": direction,
            })
    chart["cards"] = list(cards.values())
    chart["curves"] = list(curves.values())
    return CallToolResult(content=[TextContent(type="text", text="Charts for " + job_id)], structured_content=chart)


SERVER_INSTRUCTIONS = """PolyPaths pricing assistant. Flow:
Skip routine progress narration. Show parameter confirmation, final findings, or a problem requiring user attention.
1. list_assets(pricing_date) to see the Cusip values in the input, then prepare_base(cusip, pricing_date). It returns analysis_id and job_id with status prepared.
2. execute_job(job_id). The server shows the user a Run / Cancel form with the saved parameters; the job runs only if the user selects Run. Do not ask the user again in chat. If cancelled, stop. The call waits for BatchCal and returns the final status and results, with progress notifications while waiting. A run takes a few minutes, a nine-point curve longer. Do not call execute_job again for the same job. Do not ask the user whether to keep waiting.
3. When execute_job returns completed, use its results directly. If comparison is nonempty, call show_job_chart(job_id) for HTML comparison cards or SVG rate curves, then give a brief finding; do not duplicate the full table unless requested. If comparison is empty, reproduce display_table as a normal Markdown table in chat, preserving its decimal places; do not call show_job_chart or say that no chart was returned. Do not add a Unit column, unit suffixes or extra decimal places. read_job_results can retrieve saved results later. If the tool call is unexpectedly interrupted, read_job_status can diagnose the saved job; do not assume cancellation stopped BatchCal or restart it automatically.
4. Scenarios need a completed Base: prepare_prepay, prepare_rate_shift or prepare_curve with the same analysis_id, then execute_job and display its completed results. Rate and Curve reuse the completed Base PF with /nc; Prepay uses the saved Base settings with its multiplier. Shocks do not accumulate. If created is false, no scenario was created: follow base_status and reason. If prepared, execute the Base and prepare the requested scenario again after it completes. If running, read_job_status can check the existing run; do not execute it again. If failed, report base_error and stop; do not retry that failed job or create a new Base unless the user requests it.
Reporting: display Price with 2 decimals, OAS with 0 decimals, OA Dur with 1 decimal, OA CPR with 1 decimal and LT. CPR with 1 decimal. Display precision does not change raw calculation values. For prepay jobs the difference is the prepay run minus the Base job. For rate jobs it is scenario minus the zero-shift value inside the same job. prepay_factor is the Total Smm Asset Multiplier, not a CPR change. Keep DV01 as the engine reports it. Report rows of the same Cusip separately. A missing metric means the engine did not return it; do not invent numbers. Dates are YYYYMMDD; use Cusip values exactly as listed."""

mcp = MCPServer("polypaths-wrapper", instructions=SERVER_INSTRUCTIONS, extensions=[apps])


@mcp.tool(description="Read job numbers. If comparison is nonempty, call show_job_chart for HTML comparison cards or SVG rate curves and give a brief finding without duplicating the full table unless requested. Otherwise reproduce display_table as a normal chat table with exact decimals and no added units. Follow how_to_read; do not invent missing metrics.")
async def read_job_results(job_id: str) -> CallToolResult:
    results = agent.read_job_results(job_id)
    if results["status"] == "completed" and results["operation"] != "curve":
        results["display_table"] = agent.results_table(results)
    content = [TextContent(type="text", text=json.dumps(results))]
    return CallToolResult(content=content, structured_content=results)


@mcp.tool(description="List available TBA Cusip values for a YYYYMMDD pricing date.")
async def list_assets(pricing_date: Annotated[str, Field(pattern=r"^\d{8}$")]) -> list[dict]:
    return agent.list_assets(pricing_date)


@mcp.tool(description="Prepare one Cusip's Base valuation. Check the Cusip with list_assets first. Returns analysis_id and job_id with status prepared; nothing runs until execute_job.")
async def prepare_base(cusip: str, pricing_date: Annotated[str, Field(pattern=r"^\d{8}$")]) -> dict:
    return agent.prepare_base(cusip, pricing_date)


@mcp.tool(description="Prepare a prepay run: the saved Base settings with Total Smm Asset Multiplier set to the given factor, same calculation as Base, compared with the Base job. Needs a completed Base job; otherwise returns created false. Nothing runs until execute_job.")
async def prepare_prepay(analysis_id: str, prepay_factor: Literal[1.0, 1.5, 2.0]) -> dict:
    return agent.prepare_prepay(analysis_id, prepay_factor)


@mcp.tool(description="Prepare a parallel Govt/Libor/Agency shift in bp (zero-shift plus the given shift) using the completed Base job's PF with /nc and the saved analysis settings. Otherwise returns created false. Nothing runs until execute_job.")
async def prepare_rate_shift(analysis_id: str, shift_bp: Literal[-100, -75, -50, -25, 0, 25, 50, 75, 100]) -> dict:
    return agent.prepare_rate_shift(analysis_id, shift_bp)


@mcp.tool(description="Prepare the fixed nine-point rate sensitivity curve, -100 to +100 bp, using the completed Base job's PF with /nc and the saved analysis settings. Otherwise returns created false. One scenario run covers all nine points. Nothing runs until execute_job.")
async def prepare_curve(analysis_id: str) -> dict:
    return agent.prepare_curve(analysis_id)


class RunConfirmation(BaseModel):
    choice: Literal["Run", "Cancel"] = Field(title="Execute this run?")


@mcp.tool(description="Review saved parameters in a user confirmation form, then execute only if the user selects Run. Waits for calculation and returns final status and result numbers; sends progress while waiting. Use comparison and display_table just like read_job_results. Do not ask whether to keep waiting or call execute_job again. Call directly after preparing; do not ask for another chat confirmation or use confirm_run.py. Cancelling the confirmation leaves the task prepared; stop without retrying.")
async def execute_job(job_id: str, ctx: Context) -> dict:
    status = agent.read_job_status(job_id)
    if status["status"] != "prepared":
        return (await read_job_results(job_id)).structured_content
    job = agent.read_job(job_id)
    settings = agent.read_json(agent.job_file(job_id).parent / "settings.json")
    preview = {
        "Cusip": status["cusip"],
        "pricing_date": status["pricing_date"],
        "operation": status["operation"],
        "input_rows": status["input_rows"],
        "calculate": scen_settings(settings)["calculate"],
        "parameters": job["parameters"],
        "scenarios": job["scens"],
        "job_id": job_id,
    }
    confirmation = await ctx.elicit(
        message="Review this run before execution. Scenario changes are relative to Base; rate shifts are in bp. prepay_factor is the Total Smm Asset Multiplier, not a CPR multiplier.\n\n"
        + json.dumps(preview, ensure_ascii=False, indent=2),
        schema=RunConfirmation,
    )
    if confirmation.action != "accept" or confirmation.data.choice != "Run":
        return {**status, "confirmation": "cancelled"}
    started = asyncio.get_running_loop().time()
    execution = asyncio.create_task(asyncio.to_thread(agent.execute_job, job_id))
    while not execution.done():
        done, _ = await asyncio.wait({execution}, timeout=20)
        if not done:
            elapsed = asyncio.get_running_loop().time() - started
            await ctx.report_progress(elapsed, message="BatchCal still running; elapsed " + str(int(elapsed)) + " seconds.")
    await execution
    return (await read_job_results(job_id)).structured_content


@mcp.tool(description="Read saved task status: prepared, running, completed or failed. Useful for diagnosis or recovery after an interrupted tool call, including finalizing old jobs with a finished marker. Normal execute_job calls wait for completion without status polling. Never starts or restarts BatchCal.")
async def read_job_status(job_id: str) -> dict:
    return agent.read_job_status(job_id)


if __name__ == "__main__":
    mcp.run()
