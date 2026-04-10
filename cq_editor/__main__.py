import os
import sys
import argparse
from pathlib import Path

from PyQt5.QtWidgets import QApplication

NAME = "CQ-editor"


def prepare_runtime_env():

    os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")

    configured = os.environ.get("IPYTHONDIR")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((Path.home() / ".ipython", Path.home() / ".cq-editor" / "ipython"))

    for base in candidates:
        try:
            profile = base / "profile_default"
            profile.mkdir(parents=True, exist_ok=True)
            probe = profile / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            os.environ["IPYTHONDIR"] = str(base)
            break
        except OSError:
            continue


prepare_runtime_env()

# need to initialize QApp here, otherewise svg icons do not work on windows
app = QApplication(sys.argv, applicationName=NAME)

from .main_window import MainWindow


def main():

    parser = argparse.ArgumentParser(description=NAME)
    parser.add_argument("filename", nargs="?", default=None)

    args = parser.parse_args(app.arguments()[1:])

    # sys.exit(app.exec_())

    try:
        win = MainWindow(filename=args.filename if args.filename else None)
        win.show()
        app.exec_()
    except Exception as e:
        import traceback

        traceback.print_exc()


if __name__ == "__main__":

    main()
