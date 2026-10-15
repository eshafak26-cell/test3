import copy
import glob
import io
import json
import os
import shutil
import subprocess

import pandas as pd

def read_json(path):
    return json.load(io.open(path, encoding="utf-8"))


def read_input(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def write_input(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, lineterminator="\r\n")
    return path


def merge(base, extra):
    result = copy.deepcopy(base)
    for name in extra:
        if isinstance(extra[name], dict) and isinstance(result.get(name), dict):
            result[name] = merge(result[name], extra[name])
        else:
            result[name] = copy.deepcopy(extra[name])
    return result


def setting_to_args(name, value, names):
    if name == "calculate":
        return value.split()
    if isinstance(value, list) or isinstance(value, dict) or value == "":
        return []
    if name in names["ppconfig"] or name in names["ppconfig_local"]:
        return ["/ppconfig", name + "=" + str(value)]
    if name in names["setfield"]:
        return ["/setfield", name + "=" + str(value)]
    if name in names["flag"]:
        if value is True:
            return ["/" + name]
        if value is False:
            return []
        return ["/" + name, str(value)]
    return []


def write_cmd_file(settings, cmds):
    os.makedirs(os.path.dirname(settings["cmd_file"]), exist_ok=True)
    text = "REM run_type: " + settings["run_type"] + "    mode: " + settings["mode"] + "    positions: " + settings["positions"] + "\n"
    for step in cmds:
        cmd = cmds[step]
        lines = []
        for arg in cmd[:-2]:
            if arg.startswith("/") or lines == []:
                lines.append([arg])
            else:
                lines[-1].append(arg)
        lines.append([cmd[-2], cmd[-1]])
        text = text + "\nREM " + step + "\n" + " ^\n".join(subprocess.list2cmdline(line) for line in lines) + "\n"
    print(text)
    io.open(settings["cmd_file"], "w", encoding="utf-8").write(text)
    return text


def run_cmds(cmds, run_log=None):
    for step in cmds:
        cmd = cmds[step]
        if run_log is not None:
            run_log.write("\n" + step + "\n" + subprocess.list2cmdline(cmd) + "\n")
            run_log.flush()
        if step == "copy_rule":
            for rule_file in glob.glob(cmd[2]):
                shutil.copy(rule_file, cmd[3])
        else:
            subprocess.run(cmd, stdout=run_log, stderr=run_log)
