"""Make direct script execution use this checkout's src package."""

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def setup_source_path():
    project_root = Path(__file__).resolve().parent
    if sys.version_info < (3, 10):
        raise SystemExit(
            "This project requires Python 3.10 or newer.\n"
            "Current interpreter: {} ({}).\n"
            "Activate the agent environment, then run {}.\n"
            "Or use start_career_gui.cmd / start_career.cmd in this directory."
            .format(sys.executable, sys.version.split()[0], Path(sys.argv[0]).name)
        )
    source = str(project_root / "src")
    if source in sys.path:
        sys.path.remove(source)
    sys.path.insert(0, source)
    if "--check" in sys.argv[1:]:
        versions = {}
        for module in ("openai", "dotenv", "pypdf"):
            try:
                loaded = importlib.import_module(module)
            except ImportError as error:
                raise SystemExit(
                    'Missing dependency {} in {}.\nPowerShell: & "{}" -m pip install -e "{}"'
                    .format(module, sys.executable, sys.executable, project_root)
                ) from error
            versions[module] = getattr(loaded, "__version__", "installed")
        package = importlib.import_module("deepseek_agent")
        print(json.dumps(dict(python=sys.executable, source=package.__file__,
                              dependencies=versions), ensure_ascii=True))
        raise SystemExit(0)


def launch_agent(script, arguments):
    if script not in ("career.py", "career_gui.py"):
        raise SystemExit("Unsupported entry script.")
    candidates = []
    if os.environ.get("CAREER_PYTHON"):
        candidates.append(Path(os.environ["CAREER_PYTHON"]))
    prefix = Path(sys.prefix)
    if prefix.name.lower() == "agent":
        candidates.append(prefix / "python.exe")
    roots = [prefix, Path(sys.executable).parent]
    if prefix.parent.name.lower() == "envs":
        roots.append(prefix.parent.parent)
    conda = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if conda:
        directory = Path(conda).parent
        roots.append(directory.parent if directory.name.lower() in ("scripts", "condabin") else directory)
    for root in roots:
        candidates.append(root / "envs" / "agent" / "python.exe")
    interpreter = next((candidate for candidate in candidates if candidate.is_file()), None)
    if interpreter is None:
        raise SystemExit("Cannot find the agent environment. Set CAREER_PYTHON to its python.exe path.")
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment["PATH"] = os.pathsep.join([str(interpreter.parent),
                                           str(interpreter.parent / "Scripts"),
                                           str(interpreter.parent / "Library" / "bin"),
                                           environment.get("PATH", "")])
    project = Path(__file__).resolve().parent
    return subprocess.call([str(interpreter), str(project / script)] + arguments,
                           cwd=str(project), env=environment)


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--launch":
    raise SystemExit(launch_agent(sys.argv[2], sys.argv[3:]))
