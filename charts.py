import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgba
from matplotlib.offsetbox import AnnotationBbox, HPacker, TextArea
from matplotlib.ticker import StrMethodFormatter

from agent import METRIC_DECIMALS, METRICS, format_metric


def draw_comparison(job_dir, job, comparison):
    groups = {}
    for result in comparison:
        group_key = (result["row"], result["cusip"])
        groups.setdefault(group_key, []).append(result)

    plot_files = []
    for group_number, (group_key, results) in enumerate(groups.items()):
        row_number, cusip = group_key
        plot_file = job_dir / ("comparison_" + str(group_number + 1) + ".png")
        if job["operation"] == "curve":
            draw_curve(plot_file, cusip, job, results)
        else:
            draw_card(plot_file, cusip, job, results)
        plot_files.append(str(plot_file))
    return plot_files


def draw_card(plot_file, cusip, job, results):
    scen_name = results[0]["scenario"]
    metrics = []
    for metric in METRICS:
        for result in results:
            if result["metric"] == metric:
                metrics.append(result)
    figure, axes = plt.subplots(2, 3, figsize=(8.5, 3.2))
    figure.text(0.025, 0.96, cusip + " — Base vs " + scen_name, fontsize=11.5, fontweight="bold", va="top", color="#172033")
    accents = ("#5275d9", "#d88b49", "#c6a23d", "#8061bd", "#329b88", "#518cae")
    for axis, result, accent in zip(axes.flat, metrics, accents):
        axis.set_axis_off()
        axis.add_patch(plt.Rectangle((0.01, 0.04), 0.98, 0.92, transform=axis.transAxes, facecolor="white", edgecolor="#e8ebef", linewidth=0.7))
        axis.plot([0.01, 0.01], [0.04, 0.96], transform=axis.transAxes, color=accent, linewidth=1.5)
        axis.text(0.07, 0.79, result["metric"], transform=axis.transAxes, fontsize=9.5, color="#69717e")
        base_value = format_metric(result["metric"], result["base"])
        scen_value = format_metric(result["metric"], result["value"])
        value_size = 9.5 if max(len(base_value), len(scen_value)) > 7 else 11.5
        values = HPacker(children=[
            TextArea(base_value, textprops={"fontsize": value_size, "color": "#69717e"}),
            TextArea("→", textprops={"fontsize": 9.5, "color": "#a0a7b2"}),
            TextArea(scen_value, textprops={"fontsize": value_size, "fontweight": "bold", "color": "#172033"}),
        ], align="baseline", pad=0, sep=4)
        axis.add_artist(AnnotationBbox(values, (0.07, 0.48), xycoords="axes fraction", box_alignment=(0, 0.5), frameon=False))
        difference = result["difference"]
        sign = "+" if difference > 0 else ""
        axis.text(0.07, 0.16, sign + format_metric(result["metric"], difference), transform=axis.transAxes, fontsize=8, color=accent,
                  bbox={"boxstyle": "round,pad=0.25", "facecolor": to_rgba(accent, 0.1), "edgecolor": "none"})
    for axis in axes.flat[len(metrics):]:
        axis.set_axis_off()
    figure.text(0.025, 0.025, "Base = " + describe_base(job) + "   |   " + describe_scenario(job, scen_name), fontsize=7, color="#8a929f")
    figure.tight_layout(pad=0.6, rect=(0, 0.07, 1, 0.9))
    figure.savefig(plot_file, dpi=130)
    plt.close(figure)


def draw_curve(plot_file, cusip, job, results):
    shift_by_name = {"Base": 0}
    for scen in job["scens"]:
        shift_by_name[scen["name"]] = scen["shift_bp"]
    figure, axes = plt.subplots(2, 3, figsize=(11, 6))
    figure.suptitle(cusip + "   parallel rate shift", fontsize=15, fontweight="bold")
    for axis, metric in zip(axes.flat, METRICS):
        points = [(0, None)]
        for result in results:
            if result["metric"] == metric:
                points[0] = (0, result["base"])
                points.append((shift_by_name[result["scenario"]], result["value"]))
        points.sort()
        axis.plot([p[0] for p in points], [p[1] for p in points], marker="o", color="#2563eb")
        axis.axvline(0, color="#d1d5db", linewidth=1)
        axis.set_title(metric, fontsize=11)
        axis.set_xlabel("Rate shift", fontsize=8)
        if metric in METRIC_DECIMALS:
            axis.yaxis.set_major_formatter(StrMethodFormatter("{x:,." + str(METRIC_DECIMALS[metric]) + "f}"))
        axis.grid(color="#e5e7eb")
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(plot_file, dpi=130)
    plt.close(figure)


def describe_base(job):
    return "zero-shift scenario in this job"


def describe_scenario(job, scen_name):
    for scen in job["scens"]:
        if scen["name"] == scen_name:
            if "PrepayMult" in scen:
                return "PrepayMult " + str(scen["PrepayMult"])
            if "shift_bp" in scen:
                return "Govt/Libor/Agency " + str(scen["shift_bp"])
    return scen_name
