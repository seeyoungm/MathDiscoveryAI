"""Deadline-enforcing transport for a sealed local fixture only, not a model client."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
from time import monotonic

from .pilot_contract import (MANIFEST_SHA, REQUEST_SHA, bind_response, captured_contract,
                             checked, digest, indexed_request, json_bytes, write)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path("/usr/bin/python3").resolve()
PYTHON_SHA = "889c603f0d17cb54060951bcf4c4f9b8c9ebd9e52b392c70209bbb9755d797d9"
WORKER_SHA = "3f17ed007d62b14a6e6c160ab7562a96cbfba5163e5ff15536cc879043196cda"
REQUEST_LIMIT = 8192
RESPONSE_LIMIT = 8192
CASE_NAMES = ("canonical","sleeping","malformed","oversized_response","oversized_request")
DEADLINES_S = {"canonical": 2.0, "sleeping": 0.35, "malformed": 2.0,
               "oversized_response": 2.0, "oversized_request": 2.0}


def require_socket_denial():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES):
            raise
    else:
        s.close()
        raise RuntimeError("refuse to launch fixture without operating-system socket denial")


def run_local_transport(request, menus, *, deadline_s):
    """Return a raw response only after timely exit, successful rank validation, and reap."""
    start = monotonic()
    if type(deadline_s) not in (int,float) or not 0 < deadline_s <= 3:
        raise ValueError("local fixture deadline outside permitted 0..3 seconds")
    deadline = start + deadline_s
    if type(request) is not bytes:
        raise TypeError("request must be bytes")
    base = dict(status=None, deadline_s=deadline_s, spawned=False, child_pid=None, killed=False, reaped=False,
                committed=False, returncode=None, stdout_bytes=0, request_bytes=len(request),
                request_sha256=digest(request) if len(request) <= REQUEST_LIMIT + 1 else None, response_sha256=None, network="socket_denied_before_spawn",
                fixture_executable_sha256=PYTHON_SHA, fixture_code_sha256=WORKER_SHA)
    if len(request)>REQUEST_LIMIT:
        return None, dict(base,status="REQUEST_TOO_LARGE",elapsed_s=monotonic()-start,
                          network="not_probed_no_spawn")
    require_socket_denial()
    # Execute the already-verified fixed bytes, not an adjustable command or worker path.
    worker = checked(ROOT/"tools/pilot_local_fixture.py",WORKER_SHA)
    checked(PYTHON,PYTHON_SHA)
    argv=(str(PYTHON),"-I","-S","-B","-c",worker.decode("utf-8"))
    base["fixed_argv_sha256"]=digest(json_bytes(argv))
    if monotonic() >= deadline:
        return None, dict(base,status="DEADLINE_EXCEEDED",elapsed_s=monotonic()-start)
    proc = subprocess.Popen(argv,cwd="/tmp",env={"LANG":"C","LC_ALL":"C"},shell=False,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,
                start_new_session=True)
    base.update(spawned=True,child_pid=proc.pid)
    out=bytearray()
    result=None
    status=None
    ended_io=False
    sent=0
    try:
        with selectors.DefaultSelector() as sel:
            for stream,fx in ((proc.stdin,selectors.EVENT_WRITE),(proc.stdout,selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(),False)
                sel.register(stream,fx)
            while sel.get_map() and status is None:
                remaining=deadline-monotonic()
                if remaining <= 0:
                    status="DEADLINE_EXCEEDED"
                    break
                for event, which in sel.select(remaining):
                    if monotonic() >= deadline:
                        status="DEADLINE_EXCEEDED"
                        break
                    stream=event.fileobj
                    if which==selectors.EVENT_WRITE:
                        try:
                            sent+=os.write(stream.fileno(),request[sent:])
                        except BrokenPipeError:
                            sent=len(request)
                        except BlockingIOError:
                            continue
                        if sent==len(request):
                            sel.unregister(stream)
                            stream.close()
                    else:
                        try:
                            block=os.read(stream.fileno(),min(4096,RESPONSE_LIMIT+1-len(out)))
                        except BlockingIOError:
                            continue
                        if block:
                            out.extend(block)
                            if len(out)>RESPONSE_LIMIT:
                                status="RESPONSE_TOO_LARGE"
                                break
                        else:
                            sel.unregister(stream)
                            stream.close()
            ended_io=not sel.get_map()
        if status is None and ended_io:
            remaining=deadline-monotonic()
            try:
                if remaining<=0:
                    raise subprocess.TimeoutExpired(argv,deadline_s)
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                status="DEADLINE_EXCEEDED"
            else:
                if monotonic()>=deadline:
                    status="DEADLINE_EXCEEDED"
                elif proc.returncode!=0:
                    status="NONZERO_EXIT"
                else:
                    try:
                        bind_response(bytes(out),menus)
                    except ValueError:
                        status="INVALID_RESPONSE"
                    else:
                        if monotonic()>=deadline:
                            status="DEADLINE_EXCEEDED"
                        else:
                            result=bytes(out)
                            status="COMMITTED_LOCAL_FIXTURE"
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid,signal.SIGKILL)
                base["killed"]=True
            except ProcessLookupError:
                pass
        # SIGKILL cannot be delayed by arbitrary userspace code in the fixture.
        # Cleanup occurs after the acceptance deadline; record elapsed including cleanup.
        proc.wait()
        for stream in (proc.stdin,proc.stdout):
            stream.close()
        try:
            os.waitpid(proc.pid,os.WNOHANG)
        except ChildProcessError:
            base["reaped"]=True
    finished=monotonic()
    if result is not None and (finished >= deadline or not base["reaped"]):
        result=None
        status="DEADLINE_EXCEEDED" if finished >= deadline else "REAP_UNCONFIRMED"
    base.update(status=status,elapsed_s=finished-start,stdout_bytes=len(out),returncode=proc.returncode,
                committed=result is not None,response_sha256=digest(result) if result is not None else None)
    return result,base


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    if args.out.resolve()!=(ROOT/"evidence/pilot_local_transport").resolve():
        raise ValueError("use the separate, non-overwriting local transport evidence path")
    pilot=ROOT/"evidence/week3/model_pilot"
    manifest=ROOT.parent/"records/week2/week2_smoke/dataset_manifest.jsonl"
    contract=captured_contract(checked(pilot/"request.txt",REQUEST_SHA),checked(manifest,MANIFEST_SHA))
    row,menu=contract["A0"]
    prompt=json.loads(indexed_request("A0",row,menu))
    menus={"A0":menu}
    out=args.out.resolve()
    require_socket_denial()
    out.mkdir(parents=True,exist_ok=False)
    write(out/"fixed_contract.json",dict(request_limit=REQUEST_LIMIT,response_limit=RESPONSE_LIMIT,
      case_deadlines_s=DEADLINES_S,worker_sha256=WORKER_SHA,interpreter_sha256=PYTHON_SHA,
      network="socket_denied_before_spawn",no_model_calls=True,commit_rule="timely valid rank plus clean reaped exit"))
    cases={}
    expected=("COMMITTED_LOCAL_FIXTURE","DEADLINE_EXCEEDED","INVALID_RESPONSE","RESPONSE_TOO_LARGE","REQUEST_TOO_LARGE")
    for name,status in zip(CASE_NAMES,expected):
        request=b"x"*(REQUEST_LIMIT+1) if name=="oversized_request" else json_bytes(dict(case=name,prompt=prompt))
        result,receipt=run_local_transport(request,menus,deadline_s=DEADLINES_S[name])
        if (receipt["status"]!=status or receipt["committed"]!=(name=="canonical") or
              (name!="oversized_request" and not receipt["reaped"]) or
              (name in {"sleeping","oversized_response"} and not receipt["killed"]) or
              (name=="sleeping" and receipt["stdout_bytes"]!=6) or
              (name=="oversized_request" and receipt["spawned"])):
            raise RuntimeError(f"unexpected fixture outcome: {name} {receipt}")
        cases[name]=receipt
        if result is not None:
            if result!=b'{"A0":[2,1,0]}':
                raise RuntimeError("wrong canonical ranking")
            with (out/"canonical_response.json").open("xb") as f:
                f.write(result)
    write(out/"summary.json",dict(schema="mdai-pilot-local-transport-v1",state="LOCAL_SUBPROCESS_CONTRACT_VERIFIED",
      model_calls=0,formal_gate3_pass=False,holdout_accessed=False,ranker_fit=False,cases=cases,
      fixed_contract_sha256=digest((out/"fixed_contract.json").read_bytes()),
      limits="local trusted fixture only; external model cancellation, identity, credentials, and metering not tested"))
    print("Five local transport cases: only canonical response committed; stalled child killed and reaped.")


if __name__=="__main__":
    main()
