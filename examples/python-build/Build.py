"""Example task only: TaskPort itself has no Git or build-specific behavior."""

import compileall
import json
import os
import py_compile
import subprocess
import sys
import zipfile
from pathlib import Path

inputs = json.loads(Path(os.environ["TASKPORT_INPUTS_FILE"]).read_text(encoding="utf-8"))
checkout = Path.cwd() / "checkout"
git = ["git", "-c", "core.longpaths=true"]
subprocess.run(
    [*git, "clone", "--no-hardlinks", "--no-checkout", "--", inputs["repository"], str(checkout)],
    check=True,
)
subprocess.run([*git, "-C", str(checkout), "checkout", "--detach", inputs["hash"]], check=True)
commit = subprocess.check_output(
    [*git, "-C", str(checkout), "rev-parse", "HEAD"], text=True
).strip()
if commit.lower() != inputs["hash"].lower():
    raise RuntimeError("Checked-out commit differs from requested revision")
if not compileall.compile_dir(
    checkout, quiet=1, force=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH
):
    raise SystemExit(1)
artifact = Path(os.environ["TASKPORT_OUTPUT_DIR"]) / "build.zip"
with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(checkout.rglob("*")):
        relative = path.relative_to(checkout)
        if ".git" not in relative.parts and path.is_file() and not path.is_symlink():
            archive.write(path, relative.as_posix())
Path(os.environ["TASKPORT_RESULT_FILE"]).write_text(
    json.dumps({"commit": commit, "python": sys.version, "artifact": "build.zip"}), encoding="utf-8"
)
print(f"Built {commit}")
