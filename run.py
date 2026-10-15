import json
import os
from prepare import prepare_input, write_rule_file
from tools import merge, read_json, run_cmds, setting_to_args, write_cmd_file

def run(run_type, override):
    config = read_config("config.json", run_type, override)
    settings = prepare_settings(config)
    made = make_cmd_file(settings)
    if settings["run_batchcal"]:
        run_cmds(made["cmds"])


def make_cmd_file(settings):
    settings = prepare_input(settings, names)
    scen = scen_settings(settings)
    cmds = {}
    if "horizon_date" in settings:
        target_scn = horizon_scn(settings)
        rule = write_rule_file(settings)
        cmds["copy_rule"] = ["copy", "/Y", os.path.join(settings["scenarios_folder"], settings["sector_rule"]["rule_name"] + ".*"), settings["PP_SECTOR_DIR"]]
        cmds["scn_to_csv"] = build_cmd(scn_to_csv_settings(settings, target_scn))
        cmds["csv_to_scn"] = build_cmd(csv_to_scn_settings(settings, target_scn, rule))
        scen["scn"] = target_scn
    cmds["calc"] = build_cmd(scen)
    csv_file = os.path.splitext(scen["output_file"])[0] + "_cft*.csv"
    if not scen.get("cftscn", False):
        pf_to_csv = pf_to_csv_settings(settings)
        cmds["pf_to_csv"] = build_cmd(pf_to_csv)
        csv_file = pf_to_csv["output_file"]
    os.makedirs(os.path.dirname(scen["output_file"]), exist_ok=True)
    for name in ["PPBDG_OUTPUT_PATH", "PPBDG_LOG_PATH"]:
        if settings[name] != "":
            os.makedirs(settings[name], exist_ok=True)
    if settings["DEBUG_FILE"] != "":
        os.makedirs(os.path.dirname(settings["DEBUG_FILE"]), exist_ok=True)
    cmd_text = write_cmd_file(settings, cmds)
    return {"cmd_file": settings["cmd_file"], "cmd_text": cmd_text, "csv_file": csv_file, "cmds": cmds}


def scen_settings(settings):
    scen = dict(settings)
    scen.update(settings["scenario"])
    scen["calculate"] = settings["calculate"] + " " + settings["scenario"]["calculate"]
    if "scn" in scen:
        scen["template"] = ""
    return scen


def horizon_scn(settings):
    name = os.path.splitext(os.path.basename(settings["scenario"]["scn"]))[0]
    return os.path.join(settings["scenarios_folder"], name + "_" + settings["pricing_date"] + "_" + settings["horizon_date"] + ".scn")


def scn_to_csv_settings(settings, target_scn):
    return {"BatchCal": settings["BatchCal"], "mode": "local", "scn_export": True,
            "input_file": settings["scenario"]["scn"],
            "output_file": os.path.splitext(target_scn)[0] + ".csv"}


def csv_to_scn_settings(settings, target_scn, rule):
    csv_to_scn = {"BatchCal": settings["BatchCal"], "mode": "local"}
    csv_to_scn.update(settings["horizon_scenario"])
    csv_to_scn.update({"scn_horizon_date": settings["horizon_date"], "secrule": rule, "scn_import": True,
                       "input_file": os.path.splitext(target_scn)[0] + ".csv",
                       "output_file": target_scn})
    return csv_to_scn


def pf_to_csv_settings(settings):
    csv_settings = {"BatchCal": settings["BatchCal"], "mode": "local", "nc": True}
    if "groupbyflds" in settings:
        csv_settings["groupbyflds"] = settings["groupbyflds"]
    csv_settings.update({
        "template": settings["template"],
        "input_file": settings["output_file"],
        "output_file": os.path.splitext(settings["output_file"])[0] + ".csv",
    })
    return csv_settings


def build_cmd(settings):
    cmd = [settings["BatchCal"]]
    for name in names["ppconfig"] + names["ppconfig_local"] + names["setfield"] + names["flag"]:
        if name in settings:
            cmd = cmd + setting_to_args(name, settings[name], names)
    if settings["mode"] == "remote":
        cmd = cmd + ["/remote"]
        for name in names["ppconfig"]:
            if name in settings and settings[name] != "":
                cmd = cmd + ["/remote_restart_calcs_with", name + "=" + str(settings[name])]
    cmd = cmd + [settings["input_file"], settings["output_file"]]
    return cmd


def read_config(config_path, run_type, override):
    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)

    if override == {}:
        override = config["temporary_override"]

    run_settings = config["run_type_settings"][run_type]

    positions = config["positions"]
    positions = run_settings.get("positions", positions)
    positions = override.get("positions", positions)

    mode = config["mode"]
    mode = run_settings.get("mode", mode)
    mode = override.get("mode", mode)

    pricing_date = config["pricing_date"]
    pricing_date = run_settings.get("pricing_date", pricing_date)
    pricing_date = override.get("pricing_date", pricing_date)

    run_name = config["run_name"]
    run_name = run_settings.get("run_name", run_name)
    run_name = override.get("run_name", run_name)

    output_dir = config["outputs"]["output_dir"]
    output_dir = run_settings.get("output_dir", output_dir)
    output_dir = override.get("output_dir", output_dir)

    horizon_date = config.get("horizon_date")
    horizon_date = run_settings.get("horizon_date", horizon_date)
    horizon_date = override.get("horizon_date", horizon_date)

    config["temporary_override"] = override
    config_text = json.dumps(config, ensure_ascii=False)

    config_text = config_text.replace(
        "{output_dir}", json.dumps(output_dir)[1:-1]
    )
    config_text = config_text.replace("{run_type}", run_type)
    config_text = config_text.replace("{pricing_date}", pricing_date)
    config_text = config_text.replace("{pricing_month}", pricing_date[:6])
    config_text = config_text.replace("{run_name}", run_name)
    config_text = config_text.replace("{positions}", positions)

    if horizon_date is not None:
        config_text = config_text.replace("{horizon_date}", horizon_date)

    config = json.loads(config_text)
    config["positions"] = positions
    config["mode"] = mode
    config["run_type"] = run_type
    return config


def build_common_setting(config):
    not_common = ["temporary_override", "TBA", "PORT", "remote", "run_type_settings"]
    common = {}
    for key in config:
        if key in not_common:
            continue
        if isinstance(config[key], dict):
            common.update(config[key])
        else:
            common[key] = config[key]
    return common

def prepare_settings(config):
    settings = build_common_setting(config)
    run_settings = config["run_type_settings"][config["run_type"]]

    positions = config["positions"]
    mode = config["mode"]

    settings.update(config[positions])
    settings.update(run_settings)

    if mode == "remote":
        settings.update(config["remote"])

    settings = merge(settings, config["temporary_override"])
    return settings










names = read_json("names.json")


if __name__ == "__main__":
    run_type = "base"
    override = {}
    run(run_type, override)
