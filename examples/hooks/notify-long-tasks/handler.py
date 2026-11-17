import os
import time


def handle(event_type, context):
    if context.get("tool_calls", 0) >= 10:
        path = os.path.expanduser("~/.hermes/logs/long-tasks.log")
        with open(path, "a") as fh:
            fh.write(f"{time.ctime()} session={context.get('session_id')} tools={context.get('tool_calls')}\n")
