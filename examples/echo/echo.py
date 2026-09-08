import json
import os
from pathlib import Path

inputs = json.loads(Path(os.environ["TASKPORT_INPUTS_FILE"]).read_text(encoding="utf-8"))
message = inputs["message"]
print(message)
(Path(os.environ["TASKPORT_OUTPUT_DIR"]) / "message.txt").write_text(message, encoding="utf-8")
Path(os.environ["TASKPORT_RESULT_FILE"]).write_text(
    json.dumps({"message": message, "length": len(message)}), encoding="utf-8"
)
