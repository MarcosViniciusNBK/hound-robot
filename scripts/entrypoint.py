from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


def main() -> None:
    data_dir = Path("/app/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "models").mkdir(exist_ok=True)
    (data_dir / "betfair_hist").mkdir(exist_ok=True)

    if os.geteuid() == 0:
        pw = pwd.getpwnam("appuser")
        for path in [data_dir, *data_dir.rglob("*")]:
            try:
                os.chown(path, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass
        os.environ["HOME"] = pw.pw_dir
        os.environ["USER"] = pw.pw_name
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)

    cmd = sys.argv[1:] or ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
