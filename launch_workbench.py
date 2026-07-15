from __future__ import annotations

import sys
import traceback
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def main() -> int:
    try:
        from futures_quant.dashboard import launch_dashboard

        launch_dashboard(PROJECT_ROOT)
        return 0
    except Exception:
        error = traceback.format_exc()
        report_dir = PROJECT_ROOT / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        log_path = report_dir / "workbench_launch_error.log"
        log_path.write_text(error, encoding="utf-8")
        try:
            from tkinter import Tk, messagebox

            root = Tk()
            root.withdraw()
            messagebox.showerror(
                "Quant Workbench launch failed",
                f"The error was saved to:\n{log_path}\n\n{error[-1200:]}",
            )
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
