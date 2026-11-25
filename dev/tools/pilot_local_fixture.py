"""Sealed LOCAL-ONLY test fixture, never a model client."""
import json
import sys
import time

raw = sys.stdin.buffer.read(8193)
d = json.loads(raw)
case = d["case"]
if case == "canonical":
    prompt = d["prompt"]
    key, = prompt["fixture"]
    ids = [a["index"] for a in prompt["ordered_menu"]]
    body = json.dumps({key: list(reversed(ids[:3]))}, sort_keys=True, separators=(",", ":"))
    sys.stdout.buffer.write(body.encode())
elif case == "sleeping":
    sys.stdout.buffer.write(b'{"A0":')
    sys.stdout.buffer.flush()
    time.sleep(30)
    sys.stdout.buffer.write(b'[0]}')
elif case == "malformed":
    sys.stdout.buffer.write(b'{"A0":[2],"A0":[0]}')
elif case == "oversized_response":
    sys.stdout.buffer.write(b"x" * 16384)
    sys.stdout.buffer.flush()
    time.sleep(30)
elif case == "nonzero":
    sys.exit(3)
else:
    sys.exit(4)
