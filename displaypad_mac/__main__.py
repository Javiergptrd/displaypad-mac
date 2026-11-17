"""python -m displaypad_mac <command>

  run          start the service (what the LaunchAgent runs)
  test         draw numbers 1-12 on the keys and print presses for 30 s
  preview      render every page of the config to PNG files (no device needed)
  permissions  show / request the macOS permissions the service needs
  brightness N set the backlight to N percent
  configure    open the configurator in the browser (the service must be running)
  mcp          MCP server on stdin/stdout for AI apps; forwards to the running service
"""

import argparse
import logging
import os
import shutil
import sys
import time

from . import macos
from .daemon import APP_DIR, DEFAULT_CONFIG, load_config, main as run_service
from .driver import NUM_KEYS, DisplayPad
from .render import preview_sheet, render_key, to_bgr

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_path(arg):
    path = arg or os.environ.get("DISPLAYPAD_CONFIG") or DEFAULT_CONFIG
    if not os.path.exists(path) and path == DEFAULT_CONFIG:
        os.makedirs(APP_DIR, exist_ok=True)
        shutil.copy(os.path.join(HERE, "config.example.json"), path)
        print(f"created {path} from the example config")
    return path


def cmd_test(args):
    pad = DisplayPad()
    pad.open()
    pad.set_brightness(100)
    palette = ["#E5484D", "#F76B15", "#FFC53D", "#46A758", "#12A594", "#0090FF",
               "#3E63DD", "#8E4EC6", "#D6409F", "#6F6E77", "#2B9A66", "#AD7F58"]
    for i in range(NUM_KEYS):
        pad.upload(i, to_bgr(render_key({"label": str(i + 1), "color": palette[i]})))
    print("Keys show 1-12. Press keys for the next %d seconds (Ctrl+C to stop)." % args.seconds)
    prev, end = set(), time.time() + args.seconds
    try:
        while time.time() < end:
            keys = pad.read_keys(100)
            if keys is None:
                continue
            for k in sorted(keys - prev):
                print(f"  pressed  {k + 1}")
            for k in sorted(prev - keys):
                print(f"  released {k + 1}")
            prev = keys
    except KeyboardInterrupt:
        pass
    pad.close()


def cmd_preview(args):
    cfg = load_config(_config_path(args.config))
    out_dir = args.out or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    for name, keys in cfg["pages"].items():
        path = os.path.join(out_dir, f"preview-{name}.png")
        preview_sheet(keys, cfg["base_dir"]).save(path)
        print(path)


def cmd_permissions(_args):
    im = macos.input_monitoring_status()
    ax = "granted" if macos.post_events_allowed() else "not granted"
    print(f"Input Monitoring (needed to talk to the pad):         {im}")
    print(f"Accessibility (needed for shortcuts/text/media keys): {ax}")
    if im != "granted":
        macos.request_input_monitoring()
    if ax != "granted":
        macos.request_post_events()
    app = os.path.join(sys.base_prefix, "Resources", "Python.app")
    print(f"\nProgram that needs them: {app if os.path.isdir(app) else os.path.realpath(sys.executable)}")


def cmd_brightness(args):
    pad = DisplayPad()
    pad.open()
    pad.set_brightness(args.value)
    pad.close()


def cmd_configure(args):
    import subprocess
    from . import web
    cfg_dir = os.path.dirname(os.path.abspath(_config_path(args.config)))
    subprocess.run(["open", web.configurator_url(web.load_token(cfg_dir))])


def main():
    p = argparse.ArgumentParser(prog="displaypad_mac", description="Mountain DisplayPad for macOS")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config")
    t = sub.add_parser("test")
    t.add_argument("--seconds", type=int, default=30)
    pv = sub.add_parser("preview")
    pv.add_argument("--config")
    pv.add_argument("--out")
    sub.add_parser("permissions")
    b = sub.add_parser("brightness")
    b.add_argument("value", type=int)
    c = sub.add_parser("configure", aliases=["configurar"])
    c.add_argument("--config")
    m = sub.add_parser("mcp")
    m.add_argument("--config")
    args = p.parse_args()

    if args.cmd == "mcp":  # stdout carries the protocol: nothing else may be printed there
        from .mcp import stdio_bridge
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        cfg = args.config or os.environ.get("DISPLAYPAD_CONFIG") or DEFAULT_CONFIG
        sys.exit(stdio_bridge(os.path.dirname(os.path.abspath(cfg))))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    if args.cmd == "run":
        run_service(_config_path(args.config))
    else:
        {"test": cmd_test, "preview": cmd_preview, "permissions": cmd_permissions,
         "brightness": cmd_brightness, "configure": cmd_configure, "configurar": cmd_configure}[args.cmd](args)


if __name__ == "__main__":
    main()
