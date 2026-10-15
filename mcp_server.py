import base64
import io
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

import agent

RESULTS_UI = "ui://polypaths-wrapper/results.html"

apps = Apps()
apps.add_html_resource(
    RESULTS_UI,
    io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_app.html"), encoding="utf-8").read(),
    name="results",
    title="PolyPaths results",
)


@apps.tool(resource_uri=RESULTS_UI, description="Display the existing PNG charts of a completed job inline. Call only when read_job_results returns a nonempty plots list. Base jobs use a normal table in chat and do not need this tool.")
async def show_job_chart(job_id: str) -> CallToolResult:
    results = agent.read_job_results(job_id)
    chart = {key: results[key] for key in ("job_id", "cusip", "pricing_date", "operation", "parameters", "status")}
    chart["plots"] = results.get("plots", [])
    return CallToolResult(content=[TextContent(type="text", text="Charts for " + job_id)], structured_content=chart)


@apps.tool(resource_uri=RESULTS_UI, visibility=["app"], description="Used by the results page only: returns one chart of a job as base64 PNG.")
async def get_plot(job_id: str, plot_name: str) -> CallToolResult:
    plot_paths = {os.path.basename(path): path for path in agent.read_job(job_id).get("plot_files", [])}
    plot_file = Path(plot_paths[plot_name])
    image = {"image": base64.b64encode(plot_file.read_bytes()).decode("ascii")}
    return CallToolResult(content=[TextContent(type="text", text=plot_name)], structured_content=image)


SERVER_INSTRUCTIONS = """PolyPaths pricing assistant. Flow:
Skip routine progress narration. Show parameter confirmation, final findings, or a problem requiring user attention.
1. list_assets(pricing_date) to see the Cusip values in the input, then prepare_base(cusip, pricing_date). It returns analysis_id and job_id with status prepared.
2. execute_job(job_id). The server shows the user a Run / Cancel form with the saved parameters; the job runs only if the user selects Run. Do not ask the user again in chat. If cancelled, stop.
3. When the status is completed, read_job_results(job_id). For non-curve jobs, reproduce display_table as a normal Markdown table in your chat reply, preserving its decimal places. Do not add a Unit column, unit suffixes or extra decimal places. If plots is nonempty, also call show_job_chart(job_id) to display the original PNG charts. If plots is empty, use the chat table only; do not call show_job_chart or say that no chart was returned.
4. Scenarios need a completed Base: prepare_prepay, prepare_rate_shift or prepare_curve with the same analysis_id, then execute_job and read_job_results. Each scenario starts from the saved Base settings; shocks do not accumulate. If created is false, no scenario was created: follow base_status and reason. If prepared, execute the Base and prepare the requested scenario again after it completes. If running, wait and check read_job_status; do not execute it again. If failed, report base_error and stop; do not retry that failed job or create a new Base unless the user requests it.
Reporting: display Price with 2 decimals, OAS with 0 decimals, OA Dur with 1 decimal, OA CPR with 1 decimal and LT. CPR with 1 decimal. Display precision does not change raw calculation values. For prepay jobs the difference is the prepay run minus the Base job. For rate jobs it is scenario minus the zero-shift value inside the same job. prepay_factor is the Total Smm Asset Multiplier, not a CPR change. Keep DV01 as the engine reports it. Report rows of the same Cusip separately. A missing metric means the engine did not return it; do not invent numbers. Dates are YYYYMMDD; use Cusip values exactly as listed."""

mcp = MCPServer("polypaths-wrapper", instructions=SERVER_INSTRUCTIONS, extensions=[apps])


@mcp.tool(description="Read the job's numbers for a normal table in the chat. For completed non-curve jobs, reproduce display_table with its exact decimal places and no added units. If plots is nonempty, also call show_job_chart. Follow how_to_read; do not invent missing metrics.")
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


@mcp.tool(description="Prepare a parallel Govt/Libor/Agency shift in bp (zero-shift plus the given shift) from the saved Base settings of analysis_id. Needs a completed Base job; otherwise returns created false. Nothing runs until execute_job.")
async def prepare_rate_shift(analysis_id: str, shift_bp: Literal[-100, -75, -50, -25, 0, 25, 50, 75, 100]) -> dict:
    return agent.prepare_rate_shift(analysis_id, shift_bp)


@mcp.tool(description="Prepare the fixed nine-point rate sensitivity curve, -100 to +100 bp, from the saved Base settings of analysis_id. Needs a completed Base job; otherwise returns created false. One run covers all nine points. Nothing runs until execute_job.")
async def prepare_curve(analysis_id: str) -> dict:
    return agent.prepare_curve(analysis_id)


class RunConfirmation(BaseModel):
    choice: Literal["Run", "Cancel"] = Field(title="Execute this run?")


@mcp.tool(description="Review the saved task parameters in a user confirmation form, then execute only if the user selects Run. Call directly after preparing the task; do not ask for another chat confirmation or use confirm_run.py. Cancellation leaves the task prepared; stop without retrying.")
async def execute_job(job_id: str, ctx: Context) -> dict:
    status = agent.read_job_status(job_id)
    if status["status"] != "prepared":
        return status
    job = agent.read_job(job_id)
    settings = agent.read_json(agent.job_file(job_id).parent / "settings.json")
    preview = {
        "Cusip": status["cusip"],
        "pricing_date": status["pricing_date"],
        "operation": status["operation"],
        "input_rows": status["input_rows"],
        "calculate": settings["calculate"],
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
    return agent.execute_job(job_id)


@mcp.tool(description="Read one task's status using its job_id.")
async def read_job_status(job_id: str) -> dict:
    return agent.read_job_status(job_id)


if __name__ == "__main__":
    mcp.run()
