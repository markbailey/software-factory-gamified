"""The backend's entry point, run from source (python server.py) or built into an exe by build_exe.py.

Built as an exe it first writes any missing default files beside itself (prompts/, scripts/,
machines.toml, factory.toml, see factory/paths.py) and then starts the server, which reads those
copies, so edits to them are kept across launches and rebuilds.
"""
import sys

from factory import paths


def main() -> None:
    # The machines' scripts run as `{python} script.py`; with no Python installed that is this exe.
    if paths.FROZEN and paths.run_as_python(sys.argv):
        return
    for rel in paths.unpack_defaults():
        print(f"[factory] wrote default {rel.as_posix()} to {paths.BACKEND_DIR}", flush=True)

    import uvicorn

    from factory.main import app, cfg  # loads factory.toml and machines.toml from BACKEND_DIR

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
