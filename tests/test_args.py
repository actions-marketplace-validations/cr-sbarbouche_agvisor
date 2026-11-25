import base64
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agvisor import Job, Limits, Sandbox, SandboxGone
from agvisor.sandbox import _RDP_SETUP, create_sandbox, list_sandboxes, prune_egress


def test_headless():
    a = Sandbox(limits=Limits(cpus=1, memory="512m", pids=64)).run_args()
    assert {"--runtime=runsc", "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64"} <= set(a)
    assert "--privileged" not in a and a[-2:] == ["sleep", "infinity"]
    assert "--init" in a  # a real PID 1 reaps orphans; `sleep` would leave zombies that eat the pids limit
    assert "--annotation=dev.gvisor.flag.overlay2=root:self,size=5g" in a  # default disk cap
    assert "--entrypoint=" in a  # an image ENTRYPOINT would take `sleep infinity` as its arguments
    assert a.index("--entrypoint=") < a.index("ubuntu:24.04")  # a docker flag only works before the image
    assert not any(x.startswith("--entrypoint") for x in Sandbox(gui=True).run_args())  # systemd image: its own PID 1
    a = Sandbox(limits=Limits(disk="100M"), gui=True).run_args()
    assert "--annotation=dev.gvisor.flag.overlay2=root:self,size=100M" in a  # gui too; root only, never `all:`


def test_gui_start():
    s = Sandbox(gui=True)
    a = s.run_args()
    assert "--privileged" in a and "--publish=127.0.0.1::3389" in a and a[-1] == "systemd-debian-gnome"
    assert "--init" not in a  # systemd is PID 1 here
    calls = []

    def fake(*args, input=None, **_):
        calls.append((args, input))
        return subprocess.CompletedProcess(args, 0, "127.0.0.1:49153\n[::]:49153\n", "")

    s._docker = fake
    s.start()
    chpasswd, rdp = calls[-2], calls[-1]
    assert s.rdp_port == 49153 and chpasswd[1] == f"gnome:{s.password}\n"  # sudo / console password
    # RDP checks GNOME Remote Desktop's own file (baked as gnome:gnome), not the unix password: it must get ours
    assert rdp[0][:5] == ("exec", "-i", "-u", "gnome", s.name) and rdp[0][-1].endswith("/gnome-remote-desktop/credentials.ini")
    assert rdp[1] == f"[RDP]\ncredentials={{'username': <'gnome'>, 'password': <'{s.password}'>}}\n"
    assert not any(s.password in str(a) for a in rdp[0])  # stdin only: a command line shows up in `ps`


def test_gui_setup_failure_cleans_up():
    calls, run = fake_docker(fail=(_RDP_SETUP,))  # e.g. the desktop never booted
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            create_sandbox(gui=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the RDP setup failure to propagate")
    assert calls[-1][1:3] == ["rm", "-f"]  # a fresh container that never became usable is not left running


def test_runtime():
    assert "--runtime=runsc" in Sandbox().run_args()
    assert "--runtime=runsc-gui" in Sandbox(gui=True, runtime="runsc-gui").run_args()
    for bad in ({"runtime": "--privileged"}, {"runtime": "runsc,x"}, {"runtime": ""},
                {"gui": True, "runtime": "runc"}, {"gui": True, "runtime": "io.containerd.runc.v2"}):  # gui = --privileged
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_create_sandbox_starts_once():
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch("agvisor.sandbox.subprocess.run", fake_run):
        with create_sandbox(limits=Limits(cpus=1, memory="512m", pids=64), host="ssh://u@vm") as sb:
            assert sb._running  # `with` calls start() again: must be a no-op
    assert calls[0][:3] == ["docker", "-H", "ssh://u@vm"]  # host= routes docker to the remote machine
    assert [c[3] for c in calls] == ["run", "rm"]  # one docker run, then cleanup
    assert {"--cpus=1", "--memory=512m", "--pids-limit=64"} <= set(calls[0])  # flat params reach docker
    assert not sb._running


def test_limits_reject_unlimited():
    Limits(cpus=0.5, memory="512m", pids=1)  # valid
    for ok in ("1", "100k", "512M", "5g", "1T"):  # what runsc accepted on a real VM
        Limits(disk=ok)
    bad_disk = [{"disk": d} for d in ("0", "0g", "", "1.5g", "2gb", "-1g", "lots", " 1g", "1g\n")]  # 0 = unlimited in runsc
    for bad in ({"cpus": 0}, {"memory": "0"}, {"memory": "0g"}, {"memory": "lots"}, {"pids": 0}, {"pids": -1}, *bad_disk):
        try:
            Limits(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}: docker would treat it as unlimited or fail late")


def test_lifetime_and_validation():
    a = Sandbox(lifetime=60).run_args()
    assert "--rm" in a and a[-2:] == ["sleep", "60"]
    assert "--rm" not in Sandbox().run_args()
    for bad in ({"image": "--privileged"}, {"lifetime": 0}, {"gui": True, "lifetime": 60}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def fake_docker(inspect_out="", fail=(), ps_out="", volume_owner="", vols=""):
    """Patchable subprocess.run: records commands; `fail` = words that make a command exit 1;
    `volume_owner` = the agvisor.owner label that `docker volume inspect` reports; `vols` = `docker volume ls -q`."""
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        out = {"inspect": inspect_out, "port": "127.0.0.1:49153\n", "ps": ps_out}.get(cmd[1], "")
        if cmd[1:3] == ["volume", "inspect"]:
            out = volume_owner + "\n"
        if cmd[1:3] == ["volume", "ls"]:
            out = vols
        bad = any(w in cmd for w in fail)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, out, "boom" if bad else "")

    return calls, run


def test_named_sandbox_mounts_volume():
    a = Sandbox(name="proj").run_args()
    assert "--name=agvisor-proj" in a and "--volume=agvisor-proj:/workspace" in a and "--workdir=/workspace" in a
    g = Sandbox(name="proj", gui=True).run_args()
    assert "--volume=agvisor-proj:/home/gnome" in g and not any(x.startswith("--workdir") for x in g)
    assert not any(x.startswith("--volume") for x in Sandbox().run_args())  # anonymous: nothing persisted
    for bad in ("", "-x", "a b", "a,type=bind,src=/", "a:/etc", "../x", "x" * 64):
        try:
            Sandbox(name=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted name {bad!r}")


def test_resume_and_delete():
    calls, run = fake_docker(inspect_out="false\n")  # earlier container is gone/stopped: fresh one, same volume
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
        sb.delete()
    assert [c[1:3] for c in calls] == [["volume", "create"], ["volume", "inspect"], ["inspect", "-f"], ["rm", "-f"],
                                       ["run", "-d"], ["rm", "-f"], ["volume", "ls"], ["volume", "rm"]]
    assert calls[-2][1:] == ["volume", "ls", "-q", "--filter", "label=agvisor.snap=agvisor-proj"]  # its snapshots go too
    assert calls[-1][1:] == ["volume", "rm", "-f", "agvisor-proj"]

    calls, run = fake_docker(inspect_out="true\n")  # left running: re-attach, never docker run
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
    assert [c[1] for c in calls] == ["volume", "volume", "inspect"] and sb._running


def test_snapshot_and_restore():
    snap = "agvisor_snap_proj.clean"
    calls, run = fake_docker(inspect_out="false\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
        calls.clear()
        sb.snapshot("clean")
        assert [c[1:3] for c in calls] == [["volume", "create"], ["volume", "inspect"], ["volume", "create"],
                                           ["pause", "agvisor-proj"], ["run", "--rm"], ["unpause", "agvisor-proj"]]  # frozen for the copy only
        assert calls[2][2:] == ["create", "--label=agvisor.snap=agvisor-proj", snap]  # delete() finds it by this label
        cp = calls[4]
        assert "--network=none" in cp and "--runtime=runsc" in cp and f"--volume=agvisor-proj:/from:ro" in cp
        assert f"--volume={snap}:/to" in cp  # the source is read-only, only the copy is written

        calls.clear()
        assert sb.restore("clean") is sb and sb._running
        steps = [c[1:3] for c in calls]
        assert steps.index(["rm", "-f"]) < steps.index(["run", "--rm"]) < steps.index(["run", "-d"])  # stop, copy, fresh container
        cp = calls[steps.index(["run", "--rm"])]
        assert f"--volume={snap}:/from:ro" in cp and "--volume=agvisor-proj:/to" in cp  # the other way round

    calls, run = fake_docker(fail=(snap,))  # no such snapshot: must not wipe the data dir with an empty volume
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            Sandbox(name="proj").restore("clean")
        except ValueError:
            pass
        else:
            raise AssertionError("restored a snapshot that does not exist")
    assert not any(c[1] in ("run", "rm") for c in calls)

    calls, run = fake_docker(fail=("--rm",))  # the copy dies (disk full): no half-copied snapshot is left to restore from
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            Sandbox(name="proj").snapshot("clean")
        except RuntimeError:
            pass
        else:
            raise AssertionError("swallowed a failed copy")
    assert calls[-2][1:] == ["volume", "rm", "-f", snap]
    assert calls[-1][1:3] == ["unpause", "agvisor-proj"]  # and the sandbox is not left frozen

    calls, run = fake_docker(fail=("pause",), inspect_out="false\n")  # stopped: nothing writes, copy without freezing
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        Sandbox(name="proj").snapshot("clean")
    assert not any(c[1] == "unpause" for c in calls) and any(c[1:3] == ["run", "--rm"] for c in calls)

    calls, run = fake_docker(fail=("pause",), inspect_out="true\n")  # running but can't be frozen: no copy that may be torn
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            Sandbox(name="proj").snapshot("clean")
        except RuntimeError as e:
            assert "can't pause agvisor-proj" in str(e)
        else:
            raise AssertionError("copied a running sandbox it could not pause")
    assert not any(c[1:3] == ["run", "--rm"] for c in calls) and calls[-1][1:] == ["volume", "rm", "-f", snap]

    calls, run = fake_docker()  # idle_ttl: the marker is touched after unpausing, so the watchdog does not expire the sandbox
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        Sandbox(name="proj", idle_ttl=60).snapshot("clean")
    assert calls[-2][1:3] == ["unpause", "agvisor-proj"] and calls[-1][1:] == ["exec", "agvisor-proj", "touch", "/tmp/.agvisor_active"]

    calls, run = fake_docker(volume_owner="u42")  # another owner: refused before anything is copied or wiped
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        for act in ("snapshot", "restore"):
            try:
                getattr(Sandbox(name="proj", owner="other"), act)("clean")
            except PermissionError:
                continue
            raise AssertionError(f"{act} ignored the owner")
    assert not any(c[1] in ("run", "rm") for c in calls)

    for act in ("snapshot", "restore"):  # anonymous: no volume to snapshot
        try:
            getattr(Sandbox(), act)("clean")
        except ValueError:
            continue
        raise AssertionError(f"anonymous sandbox {act}d")
    for tag in ("", "-x", "a.b", "a b", "a:/etc", "x" * 33):  # ends up in a volume name
        try:
            Sandbox(name="proj").snapshot(tag)
        except ValueError:
            continue
        raise AssertionError(f"accepted tag {tag!r}")


def test_list_and_delete_snapshot():
    prefix = "agvisor_snap_proj."
    calls, run = fake_docker(vols=f"{prefix}b\n{prefix}a-1\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        assert Sandbox(name="proj").list_snapshots() == ["a-1", "b"]
        assert calls[-1][1:] == ["volume", "ls", "-q", "--filter", "label=agvisor.snap=agvisor-proj"]
        calls.clear()
        Sandbox(name="proj").delete_snapshot("b")
        assert calls[-1][1:] == ["volume", "rm", "-f", prefix + "b"]  # -f: deleting one that is already gone is fine

    calls, run = fake_docker(volume_owner="u42")  # another owner: refused, nothing listed or removed
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        for act, args in (("list_snapshots", ()), ("delete_snapshot", ("b",))):
            try:
                getattr(Sandbox(name="proj", owner="other"), act)(*args)
            except PermissionError:
                continue
            raise AssertionError(f"{act} ignored the owner")
    assert not any(c[1:3] in (["volume", "ls"], ["volume", "rm"]) for c in calls)

    # anonymous (no volume), and a tag that would end up in a volume name
    for sb, act, args in ((Sandbox(), "list_snapshots", ()), (Sandbox(), "delete_snapshot", ("b",)),
                          (Sandbox(name="proj"), "delete_snapshot", ("a.b",))):
        try:
            getattr(sb, act)(*args)
        except ValueError:
            continue
        raise AssertionError(f"{act}{args} was accepted")


def test_failed_resume_keeps_the_container():
    calls, run = fake_docker(inspect_out="true\n", fail=("chpasswd",))
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            create_sandbox(name="proj", gui=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the chpasswd failure to propagate")
    assert "rm" not in [c[1] for c in calls]  # we re-attached to someone's container: don't destroy it


def test_idle_ttl():
    a = Sandbox(idle_ttl=60).run_args()
    assert "--rm" in a and a[-3:-1] == ["sh", "-c"] and "-lt 60 ]" in a[-1]  # PID 1 is the idle watchdog
    a = Sandbox(idle_ttl=60, lifetime=3600).run_args()  # lifetime stays a hard cap on top of the idle clock
    assert a[-5:-1] == ["timeout", "3600", "sh", "-c"]
    for bad in ({"idle_ttl": 0}, {"idle_ttl": -5}, {"idle_ttl": 60, "gui": True}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")

    sb, seen = Sandbox(idle_ttl=30), []
    sb._docker = lambda *args, **_: seen.append(args) or subprocess.CompletedProcess(args, 0, "", "")
    sb.exec("ls", timeout=10)
    assert "/tmp/.agvisor_active" in seen[0][-1] and seen[0][-1].endswith("; ls")  # exec resets the idle clock
    try:
        sb.exec("ls", timeout=30)  # could still be running when the sandbox expires
    except ValueError:
        pass
    else:
        raise AssertionError("exec timeout >= idle_ttl must be rejected")
    assert len(seen) == 1  # ...before any docker call


def test_list_sandboxes():
    assert "--label=agvisor=1" in Sandbox().run_args()  # what list_sandboxes() filters on
    assert "--label=agvisor.name=proj" in Sandbox(name="proj").run_args()
    assert not any(x.startswith("--label=agvisor.name") for x in Sandbox().run_args())

    # name, state, name label, owner label, CreatedAt, egress label: tab-separated, labels may be empty
    calls, run = fake_docker(ps_out="agvisor-proj\trunning\tproj\tu42\t2026-11-06 09:28:33 +0000 UTC\t\n"
                                    "agvisor-ab12cd34\texited\t\t\t2026-11-05 23:01:02 +0100 CET\t1\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        named, anon = list_sandboxes()
        anon.stop()  # a leaked anonymous sandbox can be found and killed
    assert calls[0][1:5] == ["ps", "-a", "--filter", "label=agvisor"] and "label=agvisor.owner=u42" not in calls[0]
    assert (named.name, named.persistent, named._running) == ("agvisor-proj", True, True)
    assert (anon.name, anon.persistent, anon._running) == ("agvisor-ab12cd34", False, False)
    assert (named.owner, anon.owner) == ("u42", None)
    assert named.created == datetime(2026, 11, 6, 9, 28, 33, tzinfo=timezone.utc)
    assert anon.created == datetime(2026, 11, 5, 23, 1, 2, tzinfo=timezone(timedelta(hours=1)))  # zone name dropped
    # it was made with egress=: stopping the handle must remove its proxy and network too
    assert [c[1:] for c in calls[-3:]] == [["rm", "-f", "agvisor-ab12cd34"], ["rm", "-f", "agvisor_proxy_ab12cd34"],
                                           ["network", "rm", "agvisor_net_ab12cd34"]]
    assert not named._has_egress


def test_owner():
    assert "--label=agvisor.owner=u42" in Sandbox(owner="u42").run_args()
    assert not any(x.startswith("--label=agvisor.owner") for x in Sandbox().run_args())
    for bad in ("", "-x", "a b", "a,b", "x" * 64):
        try:
            Sandbox(owner=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted owner {bad!r}")

    calls, run = fake_docker(volume_owner="u42")  # first or matching owner: volume labelled, container created
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(name="proj", owner="u42")
    assert calls[0][1:] == ["volume", "create", "--label=agvisor.owner=u42", "agvisor-proj"]
    assert "run" in [c[1] for c in calls]

    for who, kw in (("u99", {"owner": "u99"}), ("nobody", {})):  # another owner, or none: refused before any change
        calls, run = fake_docker(inspect_out="true\n", volume_owner="u42")
        with mock.patch("agvisor.sandbox.subprocess.run", run):
            try:
                create_sandbox(name="proj", **kw)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"{who} got u42's sandbox")
        assert {c[2] for c in calls} == {"create", "inspect"} and {c[1] for c in calls} == {"volume"}

    calls, run = fake_docker()  # anonymous: no volume, so nothing to claim
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(owner="u42")
    assert "volume" not in [c[1] for c in calls]

    calls, run = fake_docker()
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        list_sandboxes(owner="u42")
    assert calls[0][5:7] == ["--filter", "label=agvisor.owner=u42"]


def test_workspace_cap():
    for ok in ("1", "100k", "512M", "10g", "1T"):
        Limits(workspace=ok)
    for bad in ("0", "0g", "", "1.5g", "1gb", "-1g", "lots", " 1g", "1g\n"):  # docker reads size=0 as no quota at all
        try:
            Limits(workspace=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted workspace={bad!r}")
    try:
        Sandbox(limits=Limits(workspace="1g"))  # anonymous: no volume, so no cap to set
    except ValueError:
        pass
    else:
        raise AssertionError("workspace= on an anonymous sandbox")
    cap = Limits(workspace="64m")

    calls, run = fake_docker(volume_owner=" 64m")  # the volume has the cap: created with it, and the sandbox starts
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(name="proj", limits=cap)
    assert calls[0][1:] == ["volume", "create", "--opt=size=64m", "agvisor-proj"] and "run" in [c[1] for c in calls]
    calls, run = fake_docker(volume_owner="u42 64M")  # with an owner too; docker echoes the size as given, so compare ignoring case
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(name="proj", owner="u42", limits=cap)
    assert calls[0][1:] == ["volume", "create", "--label=agvisor.owner=u42", "--opt=size=64m", "agvisor-proj"]
    calls, run = fake_docker(volume_owner=" 64m")  # no cap asked for, volume capped: the cap stays, nothing refused
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(name="proj")
    assert calls[0][1:] == ["volume", "create", "agvisor-proj"] and "run" in [c[1] for c in calls]

    for have, why in ((" 128m", "another cap"), ("", "no cap: `volume create` handed back an uncapped volume")):
        calls, run = fake_docker(inspect_out="true\n", volume_owner=have)  # refused before anything is re-attached or started
        with mock.patch("agvisor.sandbox.subprocess.run", run):
            try:
                create_sandbox(name="proj", limits=cap)
            except ValueError as e:
                assert "delete() it first" in str(e), e
            else:
                raise AssertionError(f"accepted a volume with {why}")
        assert {c[1] for c in calls} == {"volume"}, calls

    def no_quota(cmd, **_):  # a docker host whose volumes aren't on xfs+pquota: only a size= is refused
        refused = cmd[1:3] == ["volume", "create"] and any(a.startswith("--opt=size=") for a in cmd)
        err = "Error response from daemon: create agvisor-proj: quota size requested but no quota support"
        return subprocess.CompletedProcess(cmd, int(refused), "", err if refused else "")

    with mock.patch("agvisor.sandbox.subprocess.run", no_quota):
        try:
            create_sandbox(name="proj", limits=cap)
        except RuntimeError as e:
            assert "pquota" in str(e) and "no quota support" in str(e), e
        else:
            raise AssertionError("a cap the host can't enforce must not be silently dropped")
        create_sandbox(name="proj")  # no cap asked for: the same host is fine

    calls, run = fake_docker(fail=("create",))  # some other `volume create` failure: not dressed up as a quota problem
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            create_sandbox(name="proj", limits=cap)
        except RuntimeError as e:
            assert str(e) == "boom", e


def test_exec_raises_when_the_sandbox_is_gone():
    sb = Sandbox()
    gone = ["Error response from daemon: container 1a2b is not running",
            "Error response from daemon: No such container: agvisor-x",  # --rm deleted it after lifetime/idle_ttl
            "OCI runtime exec failed: containerManager.WaitPID: EOF"]
    for rc, err in [(1, gone[0]), (1, gone[1]), (128, gone[2])]:
        sb._docker = lambda *a, rc=rc, err=err, **_: subprocess.CompletedProcess(a, rc, "", err)
        try:
            sb.exec("ls")
        except SandboxGone:
            continue
        raise AssertionError(f"rc={rc} {err!r} must raise SandboxGone")
    sb._stream = lambda *a, **_: subprocess.CompletedProcess(a, 1, gone[1], "")  # streaming merges stderr into stdout
    try:
        sb.exec("ls", on_output=print)
    except SandboxGone:
        pass
    else:
        raise AssertionError("streaming exec must raise SandboxGone too")
    # real command failures are returned, not raised: exit 1, a command's own "is not running", a dead-looking stdout
    for rc, out, err in [(1, "", "grep: x: No such file"), (1, "", "nginx is not running"), (0, "Error response from daemon: No such container", ""),
                         (3, "", gone[0]), (1, gone[0], "")]:
        sb._docker = lambda *a, rc=rc, out=out, err=err, **_: subprocess.CompletedProcess(a, rc, out, err)
        assert sb.exec("ls").returncode == rc


def test_exec_streams_output():
    r, w = os.pipe()
    first, got = threading.Event(), []

    def writer():  # the first chunk ends in the middle of "é": it must not come out garbled
        os.write(w, b"h\xc3")
        first.wait(5)
        os.write(w, b"\xa9llo\n")
        os.close(w)

    class Popen:
        def __init__(self, cmd, **_):
            self.cmd, self.stdout, self.returncode = cmd, os.fdopen(r, "rb"), 3

        def wait(self):
            return self.returncode

    sb = Sandbox(name="proj", host="ssh://u@vm")
    threading.Thread(target=writer).start()
    with mock.patch("agvisor.sandbox.subprocess.Popen", Popen):
        res = sb.exec("echo hi", on_output=lambda t: (got.append(t), first.set()))
    assert got == ["h", "\u00e9llo\n"] and (res.returncode, res.stdout) == (3, "h\u00e9llo\n")
    assert res.args[:3] == ["docker", "-H", "ssh://u@vm"] and res.args[3:6] == ["exec", "-i", "agvisor-proj"]
    try:
        sb.exec("cat", input="x", on_output=print)
    except ValueError:
        return
    raise AssertionError("accepted stdin together with on_output")


def test_put_and_get():
    sb, seen = Sandbox(), []
    odd = "dir/a b;rm -rf x.bin"  # the path goes through a shell: it must arrive quoted
    sb._docker = lambda *a, input=None, **_: (seen.append((a, input)), subprocess.CompletedProcess(a, 0, "", ""))[1]
    sb.put(odd, b"\x00\xff bytes")
    cmd = seen[0][0][-1]
    assert shlex.quote(odd) in cmd and base64.b64decode(seen[0][1]) == b"\x00\xff bytes"  # stdin carries the data, any bytes
    sb._docker = lambda *a, **_: subprocess.CompletedProcess(a, 1, "", "sh: write error: No space left on device")
    try:
        sb.put("x", b"1")
    except RuntimeError as e:
        assert "No space left" in str(e)
    else:
        raise AssertionError("a failed write must raise")
    sb._docker = lambda *a, **_: subprocess.CompletedProcess(a, 1, "", "Error response from daemon: No such container: agvisor-x")
    try:
        sb.put("x", b"1")
    except SandboxGone:
        pass
    else:
        raise AssertionError("a dead sandbox must raise SandboxGone")

    def stream(*chunks, rc=0):  # what the sandbox prints, as docker exec would deliver it
        def f(*a, on_output, **_):
            for c in chunks:
                on_output(c)
            return subprocess.CompletedProcess(a, rc, "".join(chunks), "")
        return f

    b64 = lambda n: base64.b64encode(b"z" * n).decode()
    sb._stream = stream("AAH/", "YWJj")  # split in the middle of the data
    assert sb.get("f") == b"\x00\x01\xffabc"
    sb._stream = stream()
    assert sb.get("empty") == b""
    sb._stream = stream(b64(30))
    assert sb.get("f", max_bytes=30) == b"z" * 30  # exactly the cap: fine
    for evil in (b64(31), "A" * 10_000, "not base64!"):  # one byte over / a sandbox that floods us / junk
        sb._stream = stream(evil)
        try:
            sb.get("f", max_bytes=30)
        except ValueError:
            continue
        raise AssertionError(f"accepted {evil[:12]!r}")
    sb._stream = stream("not a file: nope\n", rc=1)
    try:
        sb.get("nope")
    except RuntimeError as e:
        assert "not a file: nope" in str(e)
    else:
        raise AssertionError("a missing file must raise")


def test_isolated_network():
    assert "--network=agvisor-isolated" in Sandbox(network="isolated").run_args()
    assert "--network=bridge" in Sandbox().run_args()  # the default is not touched
    try:
        Sandbox(network="isolated", egress=["x.org"])  # egress= has its own network
    except ValueError:
        pass
    else:
        raise AssertionError("egress= and network='isolated' together")

    inputs = []

    def start(icc, rc=0, running=False, probe_fails=False, **kw):  # create_sandbox with a docker whose network says `icc` (and `inspect` exits `rc`)
        calls = []

        def run(cmd, **kw2):
            calls.append(cmd[1:])
            inspect = cmd[1:3] == ["network", "inspect"]
            probe = "--entrypoint=cat" in cmd  # the throwaway container that reads the default bridge's resolv.conf
            out = icc if inspect else "true\n" if running and cmd[1] == "inspect" else "nameserver 9.9.9.9\n" if probe else ""
            if cmd[1] == "exec":
                inputs.append(kw2.get("input"))
            bad = (inspect and rc) or (probe and probe_fails)
            return subprocess.CompletedProcess(cmd, 1 if bad else 0, out, "boom" if bad else "")

        with mock.patch("agvisor.sandbox.subprocess.run", run):
            try:
                create_sandbox(network="isolated", **kw)
            except RuntimeError as e:
                return calls, e
        return calls, None

    calls, err = start("false\n")  # created with traffic off, checked, then the sandbox starts on it, with working DNS
    assert err is None and [c[:2] if c[0] == "network" else c[:1] for c in calls] == \
        [["network", "create"], ["network", "inspect"], ["run"], ["run"], ["exec"]]
    assert calls[0] == ["network", "create", "-o", "com.docker.network.bridge.enable_icc=false", "--label=agvisor.isolated=1", "agvisor-isolated"]
    assert "--network=bridge" in calls[3] and calls[4][:4] == ["exec", "-i", "-u", "root"] and inputs == ["nameserver 9.9.9.9\n"]  # docker's 127.0.0.11 is dead under gVisor
    for icc, rc in (("true\n", 0), ("\n", 0), ("", 1)):  # a network of that name with traffic ON, a plain one with no option, none at all
        calls, err = start(icc, rc)
        assert err and "run" not in [c[0] for c in calls], (icc, rc, err)  # no sandbox is started on it
    calls, err = start("true\n", running=True, name="proj")  # a named one left running is re-attached: no network work, no DNS rewrite
    assert err is None and "network" not in [c[0] for c in calls] and "exec" not in [c[0] for c in calls]
    calls, err = start("false\n", probe_fails=True)  # no resolver config: the sandbox is removed, not left without DNS
    assert err and calls[-1][:2] == ["rm", "-f"], (err, calls[-1])


def test_egress_args():
    for bad in ({"egress": []}, {"egress": ["*"]}, {"egress": ["a b"]}, {"egress": ["a,b"]}, {"egress": ["1.2.3.4/33"]},
                {"egress": ["x.org:0"]}, {"egress": ["x.org:99999"]}, {"egress": ["x.org:"]}, {"egress": ["::1"]},
                {"egress": ["x.org"], "network": "none"}, {"egress": ["x.org"], "gui": True}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    a = Sandbox(name="proj", egress=["pypi.org"]).run_args()
    assert "--network=agvisor_net_proj" in a and "--env-file=/dev/stdin" in a and "--label=agvisor.egress=1" in a
    assert "--network=bridge" in Sandbox().run_args() and "--env-file=/dev/stdin" not in Sandbox().run_args()

    calls, ps_rows = [], ""

    def run(cmd, **kw):
        calls.append((cmd[1:], kw.get("input")))
        out = {"ps": ps_rows}.get(cmd[1], "")
        if cmd[1] == "inspect" and cmd[-1].startswith("agvisor_proxy_"):
            out = "172.18.0.2\n"  # the proxy's address on the private network
        return subprocess.CompletedProcess(cmd, 1 if "ubuntu:24.04" in cmd and fail else 0, out, "boom")

    fail = False
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj", egress=["pypi.org", "*.pythonhosted.org", "10.0.0.0/24:8080"])
        verbs = [c[0][:2] if c[0][0] in ("network", "volume") else c[0][:1] for c in calls]
        # orphans first, then leftovers of the same name, then proxy -> network -> attach -> its address -> sandbox
        assert verbs[4:] == [["ps"], ["network", "prune"], ["rm"], ["network", "rm"], ["run"], ["network", "create"],
                             ["network", "connect"], ["inspect"], ["exec"], ["run"]], verbs  # exec: wait until the proxy is up
        proxy, create, connect, _, box = [c for c in calls if c[0][0] in ("run", "network", "inspect") and c[0][1] not in ("rm", "prune")][-5:]
        assert create[0] == ["network", "create", "--internal", "--label=agvisor.egress=1", "agvisor_net_proj"]
        assert connect[0] == ["network", "connect", "agvisor_net_proj", "agvisor_proxy_proj"]  # no DNS alias: gVisor has no 127.0.0.11
        penv = dict(l.split("=", 1) for l in proxy[1].split())
        assert penv["AGVISOR_ALLOW"] == "pypi.org,*.pythonhosted.org,10.0.0.0/24:8080"
        benv = dict(l.split("=", 1) for l in box[1].split())
        assert benv["HTTPS_PROXY"] == benv["http_proxy"] == f"http://agvisor:{penv['AGVISOR_TOKEN']}@172.18.0.2:3128"
        assert "localhost" in benv["NO_PROXY"]
        assert not any(penv["AGVISOR_TOKEN"] in str(c[0]) for c in calls)  # stdin only: a command line shows up in `ps`
        assert "--runtime=runc" in proxy[0] and "--runtime=runsc" in box[0]  # gVisor has no embedded DNS: the proxy can't resolve names there
        assert {"--user=65534:65534", "--cap-drop=ALL", "--read-only", "--security-opt=no-new-privileges"} <= set(proxy[0])
        assert "--network=agvisor_net_proj" in box[0] and "python:3.12-slim" in proxy[0]
        assert "def parse_rules" in proxy[0][-1]  # the proxy runs the real source
        calls.clear()
        sb.stop()  # the sandbox first, then what it hangs on
        assert [c[0] for c in calls] == [["rm", "-f", "agvisor-proj"], ["rm", "-f", "agvisor_proxy_proj"],
                                         ["network", "rm", "agvisor_net_proj"]]

        calls.clear()  # an anonymous sandbox has no leftovers of its name to remove first
        create_sandbox(egress=["pypi.org"]).stop()
        seq = [c[0][:2] for c in calls]
        assert seq[6][0] == "exec" and seq[6][1].startswith("agvisor_proxy_")  # waits for the proxy, by its (random) name
        seq[6] = ["exec"]
        assert seq == [["ps", "-a"], ["network", "prune"], ["run", "-d"], ["network", "create"], ["network", "connect"],
                       ["inspect", "-f"], ["exec"], ["run", "-d"], ["rm", "-f"], ["rm", "-f"], ["network", "rm"]]
        wait = calls[6][0]  # sh -c: until the rules file exists, which the proxy writes once it listens; it is in the proxy, not the sandbox
        assert wait[0] == "exec" and wait[2:4] == ["sh", "-c"] and "/run/agvisor/rules" in wait[-1] and "seq 100" in wait[-1] and wait[-1].endswith("exit 1")
        calls.clear()  # a proxy that never comes up: an error that says why, and nothing left behind
        real = run

        def no_proxy(cmd, **kw):
            if cmd[1] == "exec" and "sh" in cmd:
                calls.append(cmd[1:])
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if cmd[1] == "logs":
                return subprocess.CompletedProcess(cmd, 0, "Traceback: boom\n", "")
            return real(cmd, **kw)

        with mock.patch("agvisor.sandbox.subprocess.run", no_proxy):
            try:
                create_sandbox(egress=["pypi.org"])
            except RuntimeError as e:
                assert "did not start" in str(e) and "boom" in str(e)
            else:
                raise AssertionError("a proxy that never listens must be an error")
        assert not any(c[0] == "run" and "ubuntu:24.04" in c for c in calls)  # the sandbox was never started

        # orphans: a proxy whose sandbox expired (--rm) is removed once it is old enough, its network pruned after it
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
        old = "2026-01-01 00:00:00 +0000 UTC"
        ps_rows = (f"agvisor_proxy_dead\tagvisor-dead\t{old}\n"  # its sandbox is gone
                   f"agvisor_proxy_live\tagvisor-live\t{old}\nagvisor-live\t1\t{old}\n"  # still has one
                   f"agvisor_proxy_new\tagvisor-new\t{now}\n")  # maybe its sandbox is just being pulled
        calls.clear()
        prune_egress()
        assert [c[0] for c in calls] == [
            ["ps", "-a", "--filter", "label=agvisor.egress", "--format", '{{.Names}}\t{{.Label "agvisor.egress"}}\t{{.CreatedAt}}'],
            ["rm", "-f", "agvisor_proxy_dead"],
            ["network", "prune", "-f", "--filter", "label=agvisor.egress", "--filter", "until=60s"]], calls
        ps_rows = ""
        fail = True  # the sandbox itself fails to start: the proxy and network must not be left behind
        calls.clear()
        try:
            create_sandbox(egress=["pypi.org"])
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the failed run to propagate")
        assert [c[0][0] for c in calls[-2:]] == ["rm", "network"] and calls[-1][0][1] == "rm"


def test_inject_args():
    secret = "Bearer sk-SECRET"
    for bad in ({"inject": {}}, {"inject": {"*.x.com": {"A": "b"}}}, {"inject": {"1.2.3.4": {"A": "b"}}}, {"inject": {"x.com:443": {"A": "b"}}},
                {"inject": {"x.com": {}}}, {"inject": {"x.com": {"Host": "evil"}}}, {"inject": {"x.com": {"Content-Length": "0"}}},
                {"inject": {"x.com": {"A B": "c"}}}, {"inject": {"x.com": {"A": f"{secret}\r\nX-Evil: 1"}}},
                {"inject": {"x.com": {"A": f"{secret}\n"}}}, {"inject": {"x.com": {"A": "caf\u00e9"}}}, {"inject": {"x.com": {"A": 1}}},
                {"inject": {"x.com": {"A": "1", "a": "2"}}}, {"inject": {"x.com": {"A": "1"}, "X.com": {"A": "2"}}},
                {"inject": {"x.com": {"A": "b"}}, "network": "none"}, {"inject": {"x.com": {"A": "b"}}, "gui": True}, {"inject": ["x.com"]}):
        try:
            Sandbox(**bad)
        except ValueError as e:
            assert secret not in str(e) and "sk-SECRET" not in str(e)  # a value never ends up in an error
            continue
        raise AssertionError(f"accepted {bad}")
    sb = Sandbox(inject={"API.x.com": {"Authorization": secret}}, egress=["pypi.org"])
    assert sb.inject == {"api.x.com": {"Authorization": secret}} and sb.egress == ["pypi.org", "api.x.com"]  # the host is allowed too
    assert Sandbox(inject={"x.com": {"A": "b"}}).egress == ["x.com"]  # inject= alone is enough

    calls = []

    def run(cmd, input=None, **_):
        calls.append((cmd[1:], input))
        return subprocess.CompletedProcess(cmd, 0, "172.18.0.2\n" if cmd[1] == "inspect" else "", "")

    with mock.patch("agvisor.sandbox.subprocess.run", run), mock.patch("agvisor.sandbox._ca.make", lambda hosts: ("CA-PEM", f"LEAF+KEY for {hosts}")):
        create_sandbox(inject={"api.x.com": {"Authorization": secret}}, egress=["pypi.org"])
    proxy = next(c for c in calls if c[0][0] == "run" and any("proxy" in a for a in c[0]))
    box = [c for c in calls if c[0][0] == "run"][-1]
    assert "--tmpfs=/tmp:rw,noexec,nosuid,size=1m,mode=1777" in proxy[0] and "--read-only" in proxy[0]  # where it loads its key
    penv = dict(l.split("=", 1) for l in proxy[1].split())
    assert json.loads(base64.b64decode(penv["AGVISOR_INJECT"])) == {"api.x.com": {"Authorization": secret}}
    assert base64.b64decode(penv["AGVISOR_TLS"]) == b"LEAF+KEY for ['api.x.com']" and penv["AGVISOR_ALLOW"] == "pypi.org,api.x.com"
    benv = dict(l.split("=", 1) for l in box[1].split())
    assert benv["SSL_CERT_FILE"] == benv["REQUESTS_CA_BUNDLE"] == benv["CURL_CA_BUNDLE"] == benv["GIT_SSL_CAINFO"] == "/usr/local/share/agvisor/bundle.pem"
    assert benv["NODE_EXTRA_CA_CERTS"] == "/usr/local/share/agvisor/ca.pem"
    assert secret not in box[1] and "AGVISOR_INJECT" not in box[1] and "AGVISOR_TLS" not in box[1]  # the sandbox never gets the credential
    assert not any(secret in str(c[0]) or penv["AGVISOR_TLS"] in str(c[0]) or penv["AGVISOR_INJECT"] in str(c[0]) for c in calls)  # stdin only: a command line shows up in `ps`
    trust = next(i for i, c in enumerate(calls) if c[1] == "CA-PEM")  # the CA is on stdin, of an exec as root in the sandbox
    assert calls[trust][0][:4] == ["exec", "-i", "-u", "root"] and trust > calls.index(box) and "cat > $d/ca.pem" in calls[trust][0][-1]
    assert [c for c in calls if c[0][0] == "run" and "ubuntu:24.04" in c[0] and "--env-file=/dev/stdin" not in c[0]] == []

    calls.clear()  # no inject: no CA, no trust step, no tmpfs
    with mock.patch("agvisor.sandbox.subprocess.run", run), mock.patch("agvisor.sandbox._ca.make", lambda hosts: 1 / 0):
        create_sandbox(egress=["pypi.org"])
    assert not any("--tmpfs=/tmp:rw,noexec,nosuid,size=1m,mode=1777" in c[0] or c[0][:2] == ["exec", "-i"] for c in calls)  # (the proxy's readiness exec has no -i)

    calls.clear()  # no cryptography package: an error before anything is started
    with mock.patch("agvisor.sandbox.subprocess.run", run), mock.patch("agvisor.sandbox._ca.make", mock.Mock(side_effect=RuntimeError("inject= needs the cryptography package"))):
        try:
            create_sandbox(inject={"x.com": {"A": "b"}})
        except RuntimeError as e:
            assert "cryptography" in str(e)
        else:
            raise AssertionError("started without a CA")
    assert not any(c[0][0] == "run" for c in calls)

    calls.clear()  # the trust step fails (an image with no CA bundle): the sandbox, its proxy and network are removed
    def run_fail(cmd, input=None, **_):
        r = run(cmd, input=input)
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == "exec" and "root" in cmd else 0, r.stdout, "no CA bundle in the image")
    with mock.patch("agvisor.sandbox.subprocess.run", run_fail), mock.patch("agvisor.sandbox._ca.make", lambda hosts: ("CA-PEM", "X")):
        try:
            create_sandbox(inject={"x.com": {"A": "b"}})
        except RuntimeError as e:
            assert "no CA bundle" in str(e)
        else:
            raise AssertionError("an untrusted CA was ignored")
    assert [c[0][:2] for c in calls[-3:]] == [["rm", "-f"], ["rm", "-f"], ["network", "rm"]]


def test_egress_inject():
    """The proxy, end to end on this machine: a client that trusts the egress CA, a fake upstream with a certificate of its own."""
    import asyncio
    import ssl

    try:
        from agvisor import _ca
        _ca.make(["x.test"])
    except RuntimeError:
        print("test_egress_inject skipped: no cryptography package")
        return
    from agvisor import egress_proxy as ep

    ca_pem, leaf_pem = _ca.make(["api.test", "evil.test"])  # what the sandbox trusts, what the proxy ends TLS with
    up_ca, up_leaf = _ca.make(["api.test"])  # the real api.test, with its own CA that the proxy verifies against
    bad_ca, bad_leaf = _ca.make(["evil.test"])  # a server that is not api.test's: must never see the credential

    def server_ctx(pem):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(pem)
            f.flush()
            ctx.load_cert_chain(f.name)
        return ctx

    seen = {"api": [], "evil": [], "plain": []}

    def upstream(name):
        async def handle(r, w):
            try:
                if name == "plain":  # not TLS: an echo
                    data = await r.read(100)
                    seen[name].append(data)
                    w.write(data)
                else:
                    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
                    seen[name].append(head)
                    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await w.drain()
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ssl.SSLError):
                pass
            finally:
                w.close()
        return handle

    async def scenario():
        ports = {}
        for name, ctx in (("api", server_ctx(up_leaf)), ("evil", server_ctx(bad_leaf)), ("plain", None)):
            srv = await asyncio.start_server(upstream(name), "127.0.0.1", 0, ssl=ctx)
            ports[name] = srv.sockets[0].getsockname()[1]

        async def dial(host, port, kind):  # whatever the name, connect to our fakes
            return await asyncio.open_connection("127.0.0.1", ports[{"api.test": "api", "evil.test": "evil"}.get(host, "plain")])

        up_ctx = ssl.create_default_context(cadata=up_ca)  # the proxy trusts the real api.test's CA only
        with mock.patch.object(ep, "_dial", dial):
            proxy = await ep.serve(ep.parse_rules(["api.test", "evil.test", "other.test"]), "pw", "127.0.0.1", 0,
                                   inject=ep.parse_inject({"api.test": {"Authorization": "Bearer REAL"}, "evil.test": {"Authorization": "Bearer REAL"}}),
                                   server_ctx=ep._server_ctx(leaf_pem), upstream_ctx=up_ctx)
            pport = proxy.sockets[0].getsockname()[1]

            async def connect(host, port=443, auth="pw"):
                r, w = await asyncio.open_connection("127.0.0.1", pport)
                w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nProxy-Authorization: Basic {base64.b64encode(f'agvisor:{auth}'.encode()).decode()}\r\n\r\n".encode())
                status = (await asyncio.wait_for(r.readline(), 5)).decode()
                while await asyncio.wait_for(r.readline(), 5) not in (b"\r\n", b""):  # the rest of the answer's head
                    pass
                return r, w, status

            async def https(host, request, ca=ca_pem):
                r, w, status = await connect(host)
                assert " 200 " in status, status
                await w.start_tls(ssl.create_default_context(cadata=ca), server_hostname=host)
                w.write(request)
                reply = await asyncio.wait_for(r.read(), 5)  # to the end: `Connection: close`
                w.close()
                return reply

            ok = b"HTTP/1.1 200 OK"
            reply = await https("api.test", b"GET /v1/x HTTP/1.1\r\nHost: api.test\r\nAuthorization: Bearer DUMMY\r\nauthorization: second\r\nAccept: */*\r\nConnection: keep-alive\r\n\r\n")
            assert reply.startswith(ok) and reply.endswith(b"ok")
            head = seen["api"].pop()
            lines = head.decode().split("\r\n")
            assert lines[0] == "GET /v1/x HTTP/1.1" and "Authorization: Bearer REAL" in lines  # set
            assert sum(l.lower().startswith("authorization:") for l in lines) == 1 and "DUMMY" not in head.decode()  # replaced, not added
            assert "Accept: */*" in lines and "Host: api.test" in lines and lines.count("Connection: close") == 1 and "Connection: keep-alive" not in lines

            reply = await https("api.test", b"POST /y HTTP/1.1\r\nHost: API.test:443\r\nContent-Length: 3\r\n\r\nabc")  # none sent: added
            assert reply.startswith(ok) and b"Authorization: Bearer REAL\r\n" in seen["api"].pop()

            for request, code in ((b"GET /x HTTP/1.1\r\nHost: evil.test\r\n\r\n", b" 403 "),  # another site on the same front end
                                  (b"GET /x HTTP/1.1\r\n\r\n", b" 403 "), (b"GET /x HTTP/1.1\r\nHost: api.test\r\nHost: api.test\r\n\r\n", b" 403 "),
                                  (b"GET http://evil.test/x HTTP/1.1\r\nHost: api.test\r\n\r\n", b" 400 "),  # absolute-form
                                  (b"GET /x HTTP/1.1\r\nHost: api.test\r\nX: a\r\n b\r\n\r\n", b" 400 "),  # header folding
                                  (b"GET /x HTTP/1.1\r\nHost: api.test\r\nnot a header\r\n\r\n", b" 400 ")):
                assert code in (await https("api.test", request)).split(b"\r\n")[0], request
            assert not seen["api"], "a refused request reached the upstream"

            r, w, status = await connect("api.test")  # a client that does not trust the egress CA: no request is made at all
            try:
                await w.start_tls(ssl.create_default_context(), server_hostname="api.test")
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError("verified a certificate it should not trust")
            assert not seen["api"]

            # a host whose certificate is not the real api.test's (here: evil.test's server, reached for a host with credentials)
            try:
                await https("evil.test", b"GET / HTTP/1.1\r\nHost: evil.test\r\n\r\n")
            except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
                pass
            assert not seen["evil"], "the credential went to a server whose certificate was not verified"

            # plain http to a host with credentials is refused, not sent in the clear
            r, w = await asyncio.open_connection("127.0.0.1", pport)
            w.write(f"GET http://api.test/x HTTP/1.1\r\nHost: api.test\r\nProxy-Authorization: Basic {base64.b64encode(b'agvisor:pw').decode()}\r\n\r\n".encode())
            reply = await asyncio.wait_for(r.read(), 5)
            assert b" 403 " in reply.split(b"\r\n")[0] and b"https" in reply and not seen["api"]
            w.close()

            # a host without credentials, and api.test on another port: blind tunnels as before
            r, w, status = await connect("other.test")
            assert " 200 " in status
            w.write(b"ping")
            assert await asyncio.wait_for(r.read(4), 5) == b"ping" and seen["plain"] == [b"ping"]
            w.close()
            r, w, status = await connect("other.test", auth="wrong")
            assert " 407 " in status
            w.close()
        proxy.close()

    with contextlib.redirect_stdout(io.StringIO()):  # the proxy logs every connection
        asyncio.run(scenario())

    # injection needs both contexts; the real main() builds them from the environment
    try:
        asyncio.run(ep.serve([], "t", inject=ep.parse_inject({"x.com": {"A": "b"}})))
    except ValueError:
        pass
    else:
        raise AssertionError("served credentials without TLS")


def test_egress_proxy():
    import asyncio
    import base64

    from agvisor import egress_proxy as p

    for e, kind, val in (("pypi.org", "name", "pypi.org"), ("*.x.org:8080", "name", "*.x.org"), ("10.0.0.5", "net", None),
                         ("10.0.0.0/24:*", "net", None), ("PyPI.org", "name", "pypi.org")):
        (k, v, _), = p.parse_rules([e])
        assert k == kind and (val is None or v == val), e
    rules = p.parse_rules(["pypi.org", "*.x.org:8080", "10.0.0.0/24", "1.2.3.4:*"])
    ok = lambda h, port=443: p.match(rules, h, port)
    assert ok("pypi.org") == "name" and ok("pypi.org", 80) == "name" and not ok("pypi.org", 22)  # 80 and 443 by default
    assert not ok("www.pypi.org") and not ok("evil-pypi.org") and not ok("pypi.org.evil.com")
    assert ok("a.x.org", 8080) == "name" and not ok("x.org", 8080) and not ok("a.x.org")  # *. = subdomains, own ports
    assert ok("10.0.0.7") == "net" and not ok("10.0.1.7") and ok("1.2.3.4", 22) == "net"
    assert not ok("::ffff:10.0.0.7") and not ok("::1")  # no IPv6 rules, and a v4-mapped one is not a way in
    for addr, public in (("8.8.8.8", True), ("10.0.0.1", False), ("127.0.0.1", False), ("169.254.169.254", False),
                         ("192.168.1.1", False), ("100.64.0.1", False), ("::ffff:10.0.0.1", False), ("::1", False)):
        assert p._public(addr) is public, addr

    async def origin(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        await w.drain()
        w.close()

    async def ask(port, data):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(data)
        await w.drain()
        out = await asyncio.wait_for(r.read(), 5)
        w.close()
        return out

    async def main():
        o = await asyncio.start_server(origin, "127.0.0.1", 0)
        op = o.sockets[0].getsockname()[1]
        srv = await p.serve(p.parse_rules([f"127.0.0.1:{op}", f"localhost:{op}"]), "tok", "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        auth = b"Proxy-Authorization: Basic " + base64.b64encode(b"agvisor:tok") + b"\r\n"
        get = lambda host, *extra: b"".join([f"GET http://{host}/x HTTP/1.1\r\nHost: h\r\n".encode(), *extra, b"\r\n"])
        assert (await ask(port, get(f"127.0.0.1:{op}", auth))).endswith(b"\r\n\r\nhi")  # plain http, allowed
        assert (await ask(port, get(f"127.0.0.1:{op}"))).startswith(b"HTTP/1.1 407")  # no password
        bad = b"Proxy-Authorization: Basic " + base64.b64encode(b"agvisor:nope") + b"\r\n"
        assert (await ask(port, get(f"127.0.0.1:{op}", bad))).startswith(b"HTTP/1.1 407")
        out = await ask(port, get("127.0.0.1:1", auth))  # not in the allowlist: says so, and says which
        assert out.startswith(b"HTTP/1.1 403") and b"127.0.0.1:1 is not in" in out
        out = await ask(port, get(f"localhost:{op}", auth))  # allowed by name, but a name may not reach a private address
        assert out.startswith(b"HTTP/1.1 403") and b"no public address" in out
        out = await ask(port, f"CONNECT 127.0.0.1:{op} HTTP/1.1\r\n".encode() + auth + b"\r\nGET / HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and out.endswith(b"hi")  # a tunnel
        assert (await ask(port, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 403")
        assert (await ask(port, b"GET / HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")  # not a proxy request
        assert (await ask(port, b"GET https://x.org/ HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")
        assert (await ask(port, b"junk\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")

    log = io.StringIO()  # the proxy logs one line per connection
    with contextlib.redirect_stdout(log):
        asyncio.run(main())
    assert "allow 127.0.0.1" in log.getvalue() and "deny localhost" in log.getvalue()


def test_jobs():
    sb, calls, answers = Sandbox(idle_ttl=60), [], []

    def fake_exec(cmd, *, timeout, on_output=None, input=None, env=None, cwd=None):
        calls.append((cmd, timeout))
        rc, out = answers.pop(0) if answers else (0, "")
        if on_output:
            on_output(out)
        return subprocess.CompletedProcess([], rc, out, "boom")

    sb.exec = fake_exec
    odd = "echo \"it's\"; $(id) `x`"  # reaches a shell as one argument
    job = sb.spawn(odd, timeout=90)
    cmd, t = calls[0]
    assert re.fullmatch(r"[0-9a-f]{12}", job.id) and f"/var/tmp/.agvisor-jobs/{job.id}" in cmd
    assert f"{shlex.quote(odd)} 90 >/dev/null 2>&1 </dev/null &" in cmd and t == 30  # detached from docker exec's pipes
    sb.spawn("x")
    assert " x 0 >/dev/null" in calls[1][0]  # no timeout
    assert Sandbox(idle_ttl=20)._short() == 13 and Sandbox()._short() == 30  # exec() refuses timeout >= idle_ttl - 6
    for bad in ({"timeout": 0}, {"timeout": -1}):
        try:
            sb.spawn("x", **bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    answers.append((1, ""))
    try:
        sb.spawn("x")
    except RuntimeError as e:
        assert "spawn failed" in str(e)
    else:
        raise AssertionError("a job that never started must raise")

    calls.clear()
    answers[:] = [(0, ""), (0, "3\n")]
    assert job.poll() is None and job.poll() == 3 and job.poll() == 3 and len(calls) == 2  # an exit code is remembered
    assert job.returncode == 3
    other = Job(sb, "0123456789ab")
    answers[:] = [(2, "")]
    for call in (other.poll, other.kill, lambda: other.wait(1)):
        answers[:] = [(2, "")]
        try:
            call()
        except LookupError:
            continue
        raise AssertionError("an unknown job must raise LookupError")
    for bad in ("../x", "", "0123456789AB", "x" * 12):
        try:
            Job(sb, bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted job id {bad!r}")

    calls.clear()
    answers[:] = [(0, ""), (0, ""), (0, "0\n")]
    assert Job(sb, "0123456789ab").wait() == 0
    assert [t for _, t in calls] == [30, 30, 30] and "seq 25" in calls[0][0]  # slices: one exec per ~25 s, under idle_ttl
    try:
        Job(Sandbox(idle_ttl=12), "0123456789ab").wait()
    except ValueError:
        pass
    else:
        raise AssertionError("a wait() that can't fit under idle_ttl must say so")
    calls.clear()
    clock = iter([0, 0, 100])  # started at 0; still running after one 10 s slice; the deadline passed by the next look
    with mock.patch("agvisor.sandbox.time.monotonic", lambda: next(clock)):
        answers[:] = [(0, ""), (0, "")]
        try:
            Job(sb, "0123456789ab").wait(10)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected TimeoutError")
    assert "seq 10" in calls[0][0] and len(calls) == 1  # the last slice is no longer than what is left

    calls.clear()
    j = Job(sb, "0123456789ab")
    j.kill()
    j.kill(force=True)
    assert "kill -s TERM -- -$(cat /var/tmp/.agvisor-jobs/0123456789ab/pid)" in calls[0][0] and "kill -s KILL" in calls[1][0]

    rs = Sandbox(idle_ttl=60)  # the real exec(), over a fake _stream
    seen = []
    rs._stream = lambda *a, on_output: (seen.append(a[-1]), on_output(base64.b64encode(b"h\xc3\xa9llo").decode()),
                                        subprocess.CompletedProcess(a, 0, "", ""))[2]
    j = Job(rs, "0123456789ab")
    assert j.read(10, 50) == "h\u00e9llo".encode()
    assert "tail -c +11 /var/tmp/.agvisor-jobs/0123456789ab/log | head -c 50 | base64 -w0" in seen[0]  # bytes, 1-based
    rs._stream = lambda *a, on_output: (on_output("no such job\n"), subprocess.CompletedProcess(a, 2, "no such job\n", ""))[1]
    try:
        j.read()
    except LookupError:
        pass
    else:
        raise AssertionError("reading an unknown job must raise LookupError")


def test_mcp_jobs():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 600

        def __init__(self):
            self.calls, self.rc, self.out, self.gone, self.stopped = [], None, b"", False, False

        def spawn(self, cmd, *, timeout):
            self.calls.append(("spawn", cmd, timeout))
            return Job(self, "0123456789ab")

        def exec(self, cmd, *, timeout, on_output=None):  # what Job.poll() / .kill() run
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            return subprocess.CompletedProcess([], 0, "" if self.rc is None else f"{self.rc}\n", "")

        def _b64(self, script, max_bytes, timeout):
            self.calls.append(("read", script))
            return self.out[:max_bytes]

        def _short(self):
            return 30

        def stop(self):
            self.stopped = True

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.start_job("sleep 99", timeout=60) == "0123456789ab" and box.calls == [("spawn", "sleep 99", 60)]
        box.out = "caf\u00e9\n".encode()
        assert m.job_output("0123456789ab") == "caf\u00e9\n\n[running; next offset 6]"  # offsets count bytes
        box.rc = 0
        assert m.job_output("0123456789ab", 6).endswith("[exit 0; next offset 12]")
        assert "invalid job id" in m.job_output("../../etc")  # the agent's string never reaches a shell path
        assert "invalid job id" in m.kill_job("x")
        assert m.kill_job("0123456789ab") == "sent TERM to job 0123456789ab"
        box.gone = True
        assert m.job_output("0123456789ab").startswith("sandbox was reset") and box.stopped and m._sb is None
    m._sb = None


def test_pool():
    import time

    from agvisor import Pool, pool as pool_mod

    class Fake:  # a sandbox: `dead` ones fail the health check the way an expired one does
        made = []

        def __init__(self, **kw):
            self.kw, self.dead, self.stopped, self.rc = kw, False, False, 0
            Fake.made.append(self)

        def _short(self):
            return 30

        def exec(self, cmd, *, timeout):
            if self.dead:
                raise SandboxGone("agvisor-x is gone")
            return subprocess.CompletedProcess([], self.rc, "", "")

        def stop(self):
            self.stopped = True

    def wait(cond):
        for _ in range(200):
            if cond():
                return
            time.sleep(0.01)
        raise AssertionError("the pool did not get there")

    delay = [0]

    def create(**kw):
        time.sleep(delay[0])
        return Fake(**kw)

    with mock.patch.object(pool_mod, "create_sandbox", create):
        p = Pool(2, "img", network="none", lifetime=60)
        wait(lambda: p.spares == 2)
        assert [f.kw for f in Fake.made] == [{"image": "img", "network": "none", "lifetime": 60}] * 2  # same arguments for all

        a = p.acquire()
        assert a is Fake.made[0] and not a.stopped
        wait(lambda: p.spares == 2 and len(Fake.made) == 3)  # a replacement was made in the background

        Fake.made[1].dead = True  # expired while waiting: skipped, and not handed out
        b = p.acquire()
        assert b is Fake.made[2] and Fake.made[1].stopped
        wait(lambda: p.spares == 2)

        Fake.made[3].rc = 1  # `true` failed: not a sandbox to hand out either
        c = p.acquire()
        assert c is Fake.made[4] and Fake.made[3].stopped

        wait(lambda: p.spares == 2)
        spares = list(p._spares)
        p.close()
        assert all(f.stopped for f in spares) and not (a.stopped or b.stopped or c.stopped)  # acquired ones are the caller's
        try:
            p.acquire()
        except RuntimeError:
            pass
        else:
            raise AssertionError("acquired from a closed pool")

        # close() while a spare is still being made: it must not be left running
        Fake.made.clear()
        delay[0] = 0.3
        p = Pool(1, lifetime=60)
        p.close()
        wait(lambda: Fake.made and Fake.made[0].stopped)
        assert p.spares == 0

        # an empty pool makes one on the spot
        p = Pool(1, lifetime=60)
        got = p.acquire()
        assert isinstance(got, Fake)
        p.close()
        wait(lambda: p._making == 0)

        # making a spare fails in the background: recorded, and acquire() makes its own (and raises if that fails too)
        calls = []

        def broken(**kw):
            calls.append(1)
            raise RuntimeError("no docker")

        with mock.patch.object(pool_mod, "create_sandbox", broken):
            p = Pool(1, lifetime=60)
            wait(lambda: p.error is not None and p._making == 0)
            assert str(p.error) == "no docker" and p.spares == 0
            try:
                p.acquire()
            except RuntimeError as e:
                assert str(e) == "no docker" and len(calls) >= 2
            else:
                raise AssertionError("swallowed a failed create")
            p.close()
            wait(lambda: p._making == 0)

        with Pool(1, idle_ttl=60) as p:  # `with` closes
            wait(lambda: p.spares == 1)
            held = p._spares[0]
        assert held.stopped

    for bad in ({"size": 0, "lifetime": 5}, {"lifetime": 5, "name": "x"}, {"lifetime": 5, "owner": "x"}, {"lifetime": 5, "gui": True},
                {}, {"lifetime": 0}):
        try:
            with mock.patch.object(pool_mod, "create_sandbox", create):
                Pool(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_put_dir_and_get_dir():
    import tarfile
    import tempfile

    sb, execs, puts = Sandbox(), [], []
    sb.exec = lambda cmd, **kw: (execs.append(cmd), subprocess.CompletedProcess([], 0, "", ""))[1]
    sb.put = lambda path, data, **kw: puts.append((path, data))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d, "proj")
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("a")
        (root / "sub" / "b.bin").write_bytes(b"\x00\xff")
        (root / "link").symlink_to("/etc/passwd")  # copied as a link, not followed: the host's file does not travel
        sb.put_dir(str(root), "/work dir")
        (tmp, data), = puts
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            ms = {m.name: m for m in t.getmembers()}
            assert {"./a.txt", "./sub/b.bin", "./link"} <= set(ms) and ms["./link"].issym() and ms["./link"].linkname == "/etc/passwd"
            assert all(m.uid == m.gid == 0 and not m.uname and not m.gname for m in ms.values())  # no local user names
            assert t.extractfile(ms["./sub/b.bin"]).read() == b"\x00\xff"
        assert tmp.startswith("/var/tmp/.agvisor-") and tmp.endswith(".tar")
        assert execs == [f"mkdir -p -- '/work dir' && tar -xf {tmp} -C '/work dir' --no-same-owner; rc=$?; rm -f {tmp}; exit $rc"]
        try:
            sb.put_dir(str(root / "a.txt"), "/x")
        except NotADirectoryError:
            pass
        else:
            raise AssertionError("a file is not a directory")

        def tar_of(*members):  # members: (name, kind, payload)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                for name, kind, payload in members:
                    ti = tarfile.TarInfo(name)
                    ti.type = {"file": tarfile.REGTYPE, "link": tarfile.SYMTYPE, "dir": tarfile.DIRTYPE}[kind]
                    if kind == "link":
                        ti.linkname = payload
                    ti.size = len(payload) if kind == "file" else 0
                    t.addfile(ti, io.BytesIO(payload) if kind == "file" else None)
            return buf.getvalue()

        sb.get = lambda path, *, max_bytes, timeout: got
        dest = Path(d, "out")
        got = tar_of((".", "dir", None), ("./ok.txt", "file", b"hi"), ("./sub", "dir", None), ("./sub/c", "file", b"c"))
        sb.get_dir("/work dir", str(dest))
        assert (dest / "ok.txt").read_bytes() == b"hi" and (dest / "sub" / "c").read_bytes() == b"c"
        assert execs[-2].startswith("[ -d '/work dir' ] ||") and execs[-2].endswith(" -C '/work dir' .") and execs[-1].startswith("rm -f ")
        try:  # a regular file outside the destination is an attack: an error, nothing extracted
            got = tar_of(("../escaped", "file", b"x"))
            sb.get_dir("/x", str(Path(d, "safe")))
        except tarfile.FilterError:
            pass
        else:
            raise AssertionError("extracted a ../ member")
        assert not Path(d, "escaped").exists()
        # links that point out are left out, and reported: absolute (a venv's python) or via ../
        got = tar_of(("./l", "link", "../../etc"), ("./l/pwn", "file", b"x"), ("./abs", "link", "/etc"), ("./ok", "file", b"ok"),
                     ("./inside", "link", "ok"))
        skipped = sb.get_dir("/x", str(Path(d, "safe2")))
        assert skipped == ["./l", "./abs"] and Path(d, "safe2", "ok").read_bytes() == b"ok"
        assert (Path(d, "safe2") / "inside").is_symlink() and not (Path(d, "safe2") / "abs").exists()  # a link that stays inside is kept
        assert Path(d, "safe2", "l", "pwn").read_bytes() == b"x" and not Path(d, "pwn").exists()  # `l` is a plain dir now: nothing left the destination
        got = tar_of(("/tmp/agvisor-escaped", "file", b"x"))  # an absolute name is made relative: it lands inside
        sb.get_dir("/x", str(Path(d, "safe")))
        assert Path(d, "safe", "tmp", "agvisor-escaped").read_bytes() == b"x" and not Path("/tmp/agvisor-escaped").exists()
        sb.get = lambda path, *, max_bytes, timeout: (_ for _ in ()).throw(ValueError("larger than max_bytes"))
        try:
            sb.get_dir("/x", str(Path(d, "big")))
        except ValueError:
            assert execs[-1].startswith("rm -f ") and not Path(d, "big").exists()  # the tarball is removed, nothing is created
        else:
            raise AssertionError("a tar over max_bytes must raise")
        sb.exec = lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "not a directory")
        try:
            sb.get_dir("/nope", str(Path(d, "x")))
        except RuntimeError as e:
            assert "not a directory" in str(e)
        else:
            raise AssertionError("a missing directory must raise")


def _running(sb):  # a handle as list_sandboxes() makes them
    sb._running = True
    sb.created = datetime.now(timezone.utc)
    return sb


def test_cli():
    from agvisor import cli

    def main(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:  # argparse errors
                rc = e.code
        return rc, out.getvalue()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    ps = ("agvisor-old\trunning\told\tu42\t2026-11-06 09:28:33 +0000 UTC\t\n"
          f"agvisor-new\texited\tnew\t\t{now}\t\n")

    def run(*argv, **kw):
        calls, fake = fake_docker(ps_out=ps, **kw)
        with mock.patch("agvisor.sandbox.subprocess.run", fake):
            rc, out = main(*argv)
        return rc, out, [c[1:] for c in calls[1:]]  # calls[0] is the `ps`

    rc, out, _ = run("ls")
    assert rc == 0 and out.splitlines() == ["agvisor-old\trunning\tu42\t2026-11-06T09:28:33+00:00",
                                           f"agvisor-new\tstopped\t-\t{now[:10]}T{now[11:19]}+00:00"]
    assert run("ls", "--owner", "u42")[0] == 0
    assert run("rm")[2] == [] and run("rm")[0] == 2  # no selector: refuses instead of removing everything
    assert run("rm", "nope")[0] == 2
    rc, out, calls = run("rm", "--older-than", "3600")  # only the old one; its volume stays
    assert (rc, out, calls) == (0, "removed agvisor-old\n", [["rm", "-f", "agvisor-old"]])
    rc, out, calls = run("rm", "agvisor-old", "--data")
    assert out == "removed agvisor-old\n" and ["volume", "rm", "-f", "agvisor-old"] in calls  # --data erases the files too
    assert run("rm", "agvisor-new", "--older-than", "3600")[1] == ""  # both conditions must hold

    def stream(self, *a, on_output):
        on_output("hi\n")
        return subprocess.CompletedProcess(a, 3, "hi\n", "")

    with mock.patch.object(Sandbox, "_stream", stream):
        calls, fake = fake_docker(ps_out=ps)
        with mock.patch("agvisor.sandbox.subprocess.run", fake):
            assert main("exec", "agvisor-old", "echo", "hi") == (3, "hi\n")  # the command's exit code, output as it comes
            assert main("exec", "agvisor-new", "echo")[0] == 2  # stopped: nothing to run in

    calls, fake = fake_docker()
    with mock.patch("agvisor.sandbox.subprocess.run", fake):
        assert main("prune")[0] == 0  # the proxies and networks of expired egress= sandboxes
    assert [c[1] for c in calls] == ["ps", "network"]

    import tempfile
    seen = []

    class Fake(Sandbox):  # the file operations, recorded
        def put(self, path, data, **kw):
            seen.append(("put", path, data))

        def put_dir(self, local, remote, **kw):
            seen.append(("put_dir", Path(local).name, remote))

        def get(self, path, **kw):
            seen.append(("get", path, kw))
            if path == "/missing":
                raise RuntimeError("get '/missing' failed (exit 1): not a file: /missing")
            return b"out"

        def get_dir(self, remote, local, **kw):
            seen.append(("get_dir", remote, kw))

        def exec(self, cmd, **kw):
            return subprocess.CompletedProcess([], 0 if "dir" in cmd else 1, "", "")

    calls, fake = fake_docker(ps_out=ps)
    with tempfile.TemporaryDirectory() as d, mock.patch("agvisor.sandbox.subprocess.run", fake), \
            mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Fake(name="old", host=None))]):
        f = Path(d, "in.txt")
        f.write_bytes(b"in")
        assert main("put", "agvisor-old", str(f), "/w/in.txt") == (0, "done\n") and seen[-1] == ("put", "/w/in.txt", b"in")
        assert main("put", "agvisor-old", d, "/w")[0] == 0 and seen[-1] == ("put_dir", Path(d).name, "/w")
        assert main("put", "agvisor-old", str(Path(d, "nope")), "/w")[0] == 2  # a local file that is not there: an error, no traceback
        out = Path(d, "out.txt")
        assert main("get", "agvisor-old", "/w/x", str(out))[0] == 0 and out.read_bytes() == b"out" and seen[-1] == ("get", "/w/x", {})
        main("get", "agvisor-old", "/w/x", str(out), "--max-bytes", "5")
        assert seen[-1] == ("get", "/w/x", {"max_bytes": 5})
        assert main("get", "agvisor-old", "/w/dir", str(Path(d, "o")))[0] == 0 and seen[-1] == ("get_dir", "/w/dir", {})
        assert main("get", "agvisor-old", "/missing", str(out))[0] == 2  # the sandbox's error becomes a usage error

    # `run`: a fresh sandbox, one command, the command's exit code, the sandbox always removed
    log = []

    class Box:
        def __init__(self, rc=0):
            self.rc = rc

        def __enter__(self):
            return self

        def __exit__(self, *_):
            log.append("stop")

        def put_dir(self, local, remote):
            log.append(("put_dir", local, remote))

        def exec(self, cmd, timeout, on_output):
            on_output("hello\n")
            log.append(("exec", cmd, timeout))
            return subprocess.CompletedProcess([], self.rc, "", "")

        def get_dir(self, remote, local):
            log.append(("get_dir", remote, local))
            if remote == "/missing":
                raise RuntimeError("not a directory")

    def create(image, **kw):
        log.append(("create", image, kw["limits"].memory, kw["egress"], kw["inject"], kw["host"], kw["lifetime"]))
        return Box(rc=7 if image == "failing" else 0)

    with mock.patch("agvisor.cli.create_sandbox", create), mock.patch.dict(os.environ, {"TOK": "Bearer s3"}):
        rc, out = main("run", "img", "--memory", "1g", "--egress", "pypi.org", "--inject", "api.x.com:Authorization=TOK",
                       "--put", ".:/workspace", "--get", "/workspace/out:out", "--timeout", "9", "--host", "ssh://ci@vm", "--", "echo", "hi")
        assert (rc, out) == (0, "hello\n")
        assert log == [("create", "img", "1g", ["pypi.org"], {"api.x.com": {"Authorization": "Bearer s3"}}, "ssh://ci@vm", 309),
                       ("put_dir", ".", "/workspace"), ("exec", "echo hi", 9), ("get_dir", "/workspace/out", "out"), "stop"]
        log.clear()
        assert main("run", "failing", "--get", "/missing:o", "--", "false")[0] == 7  # its code, and a missing output doesn't hide it
        assert log[-1] == "stop"
        assert main("run", "img", "--inject", "api.x.com:Authorization=NOPE", "--", "x")[0] == 2  # unset variable: usage error
        assert main("run", "img", "--inject", "api.x.com", "--", "x")[0] == 2


def test_mcp_server():
    from agvisor import mcp_server as m

    class Fake:  # stands in for a Sandbox
        idle_ttl = 30

        def __init__(self, gone):
            self.gone, self.calls, self.stopped, self.snaps = gone, [], False, set()

        def exec(self, cmd, *, timeout, on_output):
            self.calls.append((cmd, timeout))
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            return subprocess.CompletedProcess([], 3, "out\n", "")

        def stop(self):
            self.stopped = True

        def snapshot(self, tag):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", tag):
                raise ValueError(f"invalid snapshot tag {tag!r}")
            self.calls.append(("snapshot", tag))
            self.snaps.add(tag)

        def restore(self, tag):
            if tag not in self.snaps:
                raise ValueError(f"no snapshot {tag!r} of agvisor-proj")
            self.calls.append(("restore", tag))

        def list_snapshots(self):
            return sorted(self.snaps)

        def delete_snapshot(self, tag):
            self.calls.append(("delete_snapshot", tag))
            self.snaps.discard(tag)

    boxes, kws = [], []

    def create(*a, **kw):
        kws.append(kw)
        boxes.append(Fake(gone=len(boxes) == 1))  # the second sandbox dies on its first command
        return boxes[-1]

    m._sb = None
    with mock.patch.object(m, "create_sandbox", create), \
            mock.patch.dict(os.environ, {"AGVISOR_IDLE_TTL": "30", "AGVISOR_HOST": "ssh://u@vm"}):
        assert m.run("echo hi", timeout=999) == "out\n\n[exit 3]"
        # the agent gets no say in these: network off, host and limits from the environment, hard lifetime cap
        assert (kws[0]["network"], kws[0]["host"], kws[0]["idle_ttl"], kws[0]["lifetime"]) == ("none", "ssh://u@vm", 30, 3600)
        assert kws[0]["limits"] == Limits(cpus=2, memory="2g")
        assert boxes[0].calls == [("echo hi", 23)]  # exec() refuses timeout >= idle_ttl - 6: clamped to idle_ttl - 7
        m.run("again")
        assert len(boxes) == 1  # reused, not recreated
        m.reset()
        assert boxes[0].stopped and m._sb is None
        msg = m.run("x")  # sandbox 2 is dead: the agent is told, nothing is silently re-run
        assert msg.startswith("sandbox was reset") and boxes[1].stopped and m._sb is None and len(boxes) == 2
        assert m.run("y").endswith("[exit 3]") and len(boxes) == 3  # the next call gets a fresh one
        assert kws[0]["name"] is None  # anonymous unless AGVISOR_NAME says otherwise
    m._sb = None

    # AGVISOR_EGRESS replaces the network setting: an allowlist, not "none"/"bridge"
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), \
            mock.patch.dict(os.environ, {"AGVISOR_EGRESS": " pypi.org, *.pythonhosted.org ,", "AGVISOR_NETWORK": "bridge"}):
        m.run("x")
        assert kws[0]["egress"] == ["pypi.org", "*.pythonhosted.org"] and "network" not in kws[0]
    m._sb = None

    # AGVISOR_INJECT: credentials for the sandbox's proxy, which replace the network setting; they are the user's, not the agent's
    boxes.clear(), kws.clear()
    creds = {"api.x.com": {"Authorization": "Bearer sk-SECRET"}}
    with mock.patch.object(m, "create_sandbox", create), mock.patch.dict(os.environ, {"AGVISOR_INJECT": json.dumps(creds), "AGVISOR_NETWORK": "bridge"}):
        m.run("x")
        assert kws[0]["inject"] == creds and "egress" not in kws[0] and "network" not in kws[0]
    m._sb = None
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), mock.patch.dict(os.environ, {"AGVISOR_INJECT": json.dumps(creds), "AGVISOR_EGRESS": "pypi.org"}):
        m.run("x")
        assert kws[0]["inject"] == creds and kws[0]["egress"] == ["pypi.org"]  # both
    m._sb = None
    for bad in ("not json", "[]", '{"*.x.com": {"A": "b"}}', '{"x.com": {"Host": "SECRET-VALUE"}}', '{"x.com": {"A": "SECRET-VALUE\\n"}}'):
        with mock.patch.dict(os.environ, {"AGVISOR_INJECT": bad}):
            try:
                m._inject()
            except SystemExit as e:
                assert "SECRET-VALUE" not in str(e)  # a value is never repeated
            else:
                raise AssertionError(f"accepted AGVISOR_INJECT={bad}")

    # AGVISOR_NAME: a named sandbox, whose /workspace the snapshot tools save and roll back
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), mock.patch.dict(os.environ, {"AGVISOR_NAME": "proj"}):
        assert m.snapshot() == "snapshot checkpoint saved" and kws[0]["name"] == "proj"  # no tag: the old single slot
        assert "restarted" in m.restore()
        assert boxes[0].calls == [("snapshot", "checkpoint"), ("restore", "checkpoint")]
        assert m.list_snapshots() == "checkpoint"
        assert m.snapshot("before-pip") == "snapshot before-pip saved" and m.list_snapshots() == "before-pip\ncheckpoint"
        assert "restored /workspace to snapshot before-pip" in m.restore("before-pip")
        assert m.snapshot("c") == "snapshot c saved"  # the third and last of the default 3
        assert m.snapshot("d") == "already 3 snapshots (before-pip, c, checkpoint): delete_snapshot() one, or reuse one of these tags"
        assert "d" not in boxes[0].snaps
        m.delete_snapshot("c")
        assert m.snapshot("checkpoint") == "snapshot checkpoint saved"  # an existing tag is replaced, not a new slot
        assert m.delete_snapshot("checkpoint") == "snapshot checkpoint deleted" and m.list_snapshots() == "before-pip"
        assert m.restore("nope") == "no snapshot 'nope' of agvisor-proj"  # said to the agent, not a traceback
        assert m.snapshot("a b").startswith("invalid snapshot tag") and m.snapshot("../x").startswith("invalid snapshot tag")
        m.delete_snapshot("before-pip")
        assert m.list_snapshots() == "no snapshots"
        m._sb = None
        assert "Only /workspace is kept" in m.run("x")  # boxes[1] is the one that dies; its files are not all gone
    m._sb = None
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), mock.patch.dict(os.environ, {"AGVISOR_NAME": "proj", "AGVISOR_SNAPSHOTS": "1"}):
        m.snapshot("a")
        assert m.snapshot("b").startswith("already 1 snapshots (a)") and m.snapshot("a") == "snapshot a saved"
    m._sb = None
    for bad in ("0", "-1", "two", ""):  # checked when the server starts: "" falls back to the default
        with mock.patch.dict(os.environ, {"AGVISOR_SNAPSHOTS": bad}):
            try:
                m._snapshots()
            except SystemExit:
                assert bad != ""
            else:
                assert bad == ""


def test_mcp_write_file():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 60  # exec() refuses timeout >= idle_ttl - 6

        def __init__(self, gone):
            self.gone, self.puts, self.stopped, self.files, self.execs = gone, [], False, {}, []

        def put(self, path, data, *, timeout):
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            assert timeout == 53  # clamped under idle_ttl
            self.puts.append((path, data))

        def get(self, path, *, max_bytes, timeout):
            assert timeout == 53
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            if path not in self.files:
                raise RuntimeError(f"get {path!r} failed (exit 1): not a file: {path}")
            if len(self.files[path]) > max_bytes:
                raise ValueError("too big")
            return self.files[path]

        def exec(self, cmd, *, timeout, on_output):
            self.execs.append((cmd, timeout))
            return subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 root root 3 2026-11-09 12:00 a b.txt\n", "")

        def stop(self):
            self.stopped = True

    boxes = []
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: boxes.append(Fake(len(boxes) == 1)) or boxes[-1]):
        assert m.write_file("/tmp/a.py", "caf\u00e9") == "wrote 5 bytes to /tmp/a.py"  # bytes, not characters
        assert boxes[0].puts == [("/tmp/a.py", "caf\u00e9".encode())]
        boxes[0].files = {"/a": "caf\u00e9".encode(), "/bin": b"\x7fELF\x00\x01", "/big": b"x" * 100_001}
        assert m.read_file("/a") == "caf\u00e9"
        assert m.read_file("/bin") == "/bin is binary (6 bytes)"  # no NUL-laden text in the agent's context
        assert "bigger than 100000 bytes" in m.read_file("/big") and "not a file" in m.read_file("/nope")
        assert m.list_files("/work dir; rm x").endswith("a b.txt\n\n[exit 0]")
        assert boxes[0].execs == [("ls -lAp --time-style=long-iso -- '/work dir; rm x'", 30)]  # the path arrives quoted
        m.reset()
        assert m.write_file("/tmp/b", "x").startswith("sandbox was reset") and boxes[1].stopped and m._sb is None
    m._sb = None


def test_mcp_edit_file():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 60

        def __init__(self):
            self.files, self.stopped = {"/a": "x = 1\ny = 1\ncafé = 1\n".encode(), "/bin": b"\xff\xfe", "/big": b"x" * 1_000_001}, False

        def get(self, path, *, max_bytes, timeout):
            if path not in self.files:
                raise RuntimeError(f"get {path!r} failed (exit 1): not a file: {path}")
            if len(self.files[path]) > max_bytes:
                raise ValueError("too big")
            return self.files[path]

        def put(self, path, data, *, timeout):
            self.files[path] = data

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.edit_file("/a", "y = 1", "y = 2") == "edited /a" and box.files["/a"] == "x = 1\ny = 2\ncafé = 1\n".encode()
        assert m.edit_file("/a", "= 1", "= 3").startswith("old text appears 2 times") and b"y = 2" in box.files["/a"]  # untouched
        assert m.edit_file("/a", "nope", "x") == "old text not found in /a"
        assert m.edit_file("/a", "café", "tea") == "edited /a" and box.files["/a"].endswith(b"tea = 1\n")
        assert m.edit_file("/a", "", "x").startswith("old must not be empty")
        assert "not UTF-8" in m.edit_file("/bin", "a", "b") and box.files["/bin"] == b"\xff\xfe"
        assert "bigger than" in m.edit_file("/big", "a", "b") and "cannot edit /nope" in m.edit_file("/nope", "a", "b")
    m._sb = None


def test_python_session():
    import sys
    import tempfile

    from agvisor.sandbox import _py

    d = tempfile.mkdtemp(dir="/tmp")  # short: a unix socket path is limited to ~100 characters
    env = {**os.environ, "AGVISOR_PYDIR": f"{d}/py", "PATH": f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"}

    def local(cmd, *, input=None, timeout):  # the real command, run here with sh instead of inside a sandbox
        try:
            return subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], 124, "", "")

    sb = Sandbox()
    sb.exec = local
    try:
        r = sb.python("x = 41\nprint('hi')\nimport sys; print('err', file=sys.stderr)")
        assert r.returncode == 0 and r.stdout.startswith("[new python session") and r.stdout.endswith("hi\nerr\n"), r.stdout
        assert sb.python("x + 1").stdout == "42\n"  # same interpreter: no new-session notice, the last expression is printed
        assert sb.python("def f(n): return n * 2\n").stdout == "" and sb.python("f(x)").stdout == "82\n"
        out = sb.python("1 / 0").stdout  # an exception is a traceback, not a return code
        assert "ZeroDivisionError" in out and 'File "<python>"' in out and "_pysession" not in out, out
        assert "SyntaxError" in sb.python("def f(:").stdout and sb.python("x").stdout == "41\n"  # an error doesn't end the session
        assert "SystemExit" in sb.python("raise SystemExit(3)").stdout and sb.python("x").stdout == "41\n"
        out = sb.python("while True: pass", timeout=1).stdout  # the alarm interrupts it, the variables stay
        assert "TimeoutError: timed out after 1 s" in out and sb.python("x").stdout == "41\n", out
        assert sb.python("print('a' * 100_000)", max_bytes=1000).stdout == "a" * 999 + "\n"  # cut inside: the last 1000 bytes are all we keep
        assert sb.python("x", restart=True).stdout.startswith("[new python session") and "NameError" in sb.python("y").stdout
        # stuck where the alarm can't reach (here: ignored): exec() gives up, the session is killed
        r = sb.python("import signal; signal.signal(signal.SIGALRM, signal.SIG_IGN)\nwhile True: pass", timeout=1)
        assert r.returncode == 124
        assert sb.python("1").stdout.startswith("[new python session")
        local(_py("kill", 1000), timeout=30)
        assert not os.path.exists(f"{d}/py/sock")
        try:
            sb.python("1", timeout=0)
        except ValueError:
            pass
        else:
            raise AssertionError("timeout < 1 must be refused")
    finally:
        subprocess.run(["/bin/sh", "-c", _py("kill", 1000)], env=env, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)

    # no python3 in the image: say so, with the exit code
    bare = Sandbox()
    bare.exec = lambda cmd, *, input=None, timeout: subprocess.run(
        ["/bin/sh", "-c", cmd], input=input, capture_output=True, text=True, env={"PATH": "/nonexistent"}, timeout=timeout)
    r = bare.python("1")
    assert r.returncode == 127 and "python3 is not in this sandbox image" in r.stdout


def test_mcp_python():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 60  # exec() refuses timeout >= idle_ttl - 6

        def __init__(self):
            self.calls, self.rc, self.out, self.gone, self.stopped = [], 0, "42\n", False, False

        def python(self, code, *, timeout, restart, context=None):
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            self.calls.append((code, timeout, restart) if context is None else (code, timeout, restart, context))
            return subprocess.CompletedProcess([], self.rc, self.out, "")

        def stop(self):
            self.stopped = True

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.python("x", timeout=100) == "42\n" and box.calls[-1] == ("x", 48, False)  # clamped: exec() gives up 5 s later
        m.python("x", timeout=10, restart=True)
        assert box.calls[-1] == ("x", 10, True)
        box.out = ""
        assert m.python("x") == "(no output)"
        box.rc, box.out = 127, "python3 is not in this sandbox image"
        assert m.python("x").endswith("[exit 127]")
        box.rc = 124
        assert m.python("x", timeout=5) == "timed out after 5 s and the python session was killed: its variables are gone"
        box.gone = True
        assert m.python("x").startswith("sandbox was reset") and box.stopped and m._sb is None
    m._sb = None


def test_mcp_put_dir():
    import tempfile

    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 600

        def __init__(self, has=False, fail=False):
            self.calls, self.has, self.fail, self.stopped = [], has, fail, False

        def exec(self, cmd, *, timeout, on_output=None):
            self.calls.append(("exec", cmd))
            return subprocess.CompletedProcess([], 0 if self.has else 1, "", "")  # `[ -e dest ]`

        def put_dir(self, local, remote, *, timeout):
            self.calls.append(("put_dir", local, remote))
            if self.fail:
                raise RuntimeError("put_dir failed")

        def stop(self):
            self.stopped = True

    with tempfile.TemporaryDirectory() as d:
        bad = Fake(fail=True)
        boxes = [Fake(), Fake(has=True), bad]
        m._sb = None
        with mock.patch.object(m, "create_sandbox", lambda *a, **kw: boxes.pop(0)), \
                mock.patch.dict(os.environ, {"AGVISOR_PUT_DIR": f"{d}:/work dir/proj"}):
            m.run("echo hi")
            first = m._sb
            assert first.calls[:2] == [("exec", "[ -e '/work dir/proj' ]"), ("put_dir", d, "/work dir/proj")]
            n = len(first.calls)
            m.run("again")
            assert [c[0] for c in first.calls[n:]] == ["exec"]  # copied once, not on every call
            m.reset()
            m.run("x")  # a second sandbox that already has /work dir/proj (a resumed named volume): the agent's edits stay
            assert [c[0] for c in m._sb.calls] == ["exec", "exec"] and m._sb is not first
            m.reset()
            try:
                m.run("x")
            except RuntimeError:
                pass
            else:
                raise AssertionError("a failed copy must reach the caller")
            assert m._sb is None and bad.stopped  # and the half-made sandbox is not left running
        m._sb = None
        for spec in (f"{d}:relative/path", f"{d}/missing:/x", d, ""):
            with mock.patch.dict(os.environ, {"AGVISOR_PUT_DIR": spec}):
                try:
                    m._put()
                except SystemExit as e:
                    assert spec == "" or "HOST_DIR:/DEST" in str(e), spec
                else:
                    assert spec == "", spec  # unset is fine
        with mock.patch.dict(os.environ, {"AGVISOR_PUT_DIR": f"{d}:/dest"}):
            assert m._put() == (d, "/dest")


def test_async():
    import asyncio
    import copy
    import inspect
    import time

    from agvisor import aio

    calls = []

    def run(cmd, **_):  # a docker that takes 0.3 s per call
        calls.append(cmd)
        time.sleep(0.3)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    async def scenario():
        ticks = 0

        async def ticker():  # the event loop must keep turning while docker is slow
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        tick = asyncio.create_task(ticker())
        async with aio.AsyncSandbox(limits=Limits(cpus=1)) as sb:
            assert isinstance(sb, aio.AsyncSandbox) and sb.name.startswith("agvisor-") and sb._running  # attributes pass through
            assert calls[-1][1] == "run" and "--cpus=1" in calls[-1]  # same arguments as Sandbox
            t0, before = time.monotonic(), ticks
            rs = await asyncio.gather(*(sb.exec("x") for _ in range(5)))
            assert time.monotonic() - t0 < 1.0 and ticks - before > 10, "the calls ran one after the other, or blocked the loop"
            assert [r.returncode for r in rs] == [0] * 5 and calls[-1][1:4] == ["exec", "-i", sb.name]

            try:  # cancelling the wait: the loop is free again at once (the thread finishes by itself)
                await asyncio.wait_for(sb.exec("slow"), 0.05)
            except TimeoutError:
                pass
            else:
                raise AssertionError("did not time out")
            await asyncio.sleep(0.4)

            threads = []
            with mock.patch.object(Sandbox, "_stream", lambda self, *a, on_output: (on_output("a"), on_output("b"), subprocess.CompletedProcess(a, 0, "ab", ""))[2]):
                got = []

                async def on_async(text):
                    got.append(text)
                    threads.append(threading.current_thread() is threading.main_thread())

                assert (await sb.exec("x", on_output=on_async)).stdout == "ab" and got == ["a", "b"]
                got.clear()
                await sb.exec("x", on_output=lambda text: (got.append(text), threads.append(threading.current_thread() is threading.main_thread())))
                assert got == ["a", "b"]
            assert threads == [True, True, False, False]  # an async callback runs on the loop, a plain one in the worker thread

            seen = []
            sb._obj.exec = lambda cmd, **kw: (seen.append(cmd), subprocess.CompletedProcess([], 0, "3\n", ""))[1]
            job = await sb.spawn("x")  # a Job that is awaited, too
            assert isinstance(job, aio.AsyncJob) and re.fullmatch(r"[0-9a-f]{12}", job.id)
            assert await job.poll() == 3 and job.returncode == 3 and await job.wait() == 3
            await job.kill()

            def gone(cmd, **kw):
                raise SandboxGone("agvisor-x is gone")

            sb._obj.exec = gone
            try:
                await sb.put("/x", b"")
            except SandboxGone:
                pass
            else:
                raise AssertionError("an error in the thread must reach the caller")

            assert not hasattr(sb, "nothing") and copy.copy(sb)._obj is sb._obj  # no endless __getattr__ on a bare copy
            assert inspect.iscoroutinefunction(aio.AsyncSandbox.exec) and "timeout" in inspect.signature(aio.AsyncSandbox.exec).parameters
            assert all(inspect.iscoroutinefunction(getattr(aio.AsyncSandbox, n)) for n in ("port", "stats"))  # each runs a docker command
        assert calls[-1][1:3] == ["rm", "-f"]  # `async with` stopped it
        tick.cancel()

        with mock.patch.object(aio, "_list_sandboxes", lambda host, owner: [Sandbox(name="a", owner=owner)]):
            (one,) = await aio.list_sandboxes("ssh://u@vm", "me")
            assert isinstance(one, aio.AsyncSandbox) and one.name == "agvisor-a" and one.owner == "me"
        box = await aio.create_sandbox(host=None)
        assert isinstance(box, aio.AsyncSandbox) and box._running
        assert await box.stop() is None and not box._running

    with mock.patch("agvisor.sandbox.subprocess.run", run):
        asyncio.run(scenario())


def test_egress_proxy_idle():
    import asyncio
    import base64
    import time

    from agvisor import egress_proxy as p

    closed = asyncio.Event()

    async def origin(r, w):  # a download: five bytes 0.1 s apart, then silence with the connection left open
        for _ in range(5):
            w.write(b"x")
            await w.drain()
            await asyncio.sleep(0.1)
        await r.read()  # returns when the proxy closes its side of the tunnel
        closed.set()

    async def main():
        o = await asyncio.start_server(origin, "127.0.0.1", 0)
        op = o.sockets[0].getsockname()[1]
        srv = await p.serve(p.parse_rules([f"127.0.0.1:{op}"]), "tok", "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
        w.write(f"CONNECT 127.0.0.1:{op} HTTP/1.1\r\nProxy-Authorization: Basic ".encode() + base64.b64encode(b"agvisor:tok") + b"\r\n\r\n")
        await r.readuntil(b"\r\n\r\n")
        t0 = time.monotonic()
        body = await asyncio.wait_for(r.read(), 10)  # until the proxy ends it
        took = time.monotonic() - t0
        # the client sent nothing for the whole 0.5 s of the download, longer than _IDLE: only quiet in BOTH directions counts
        assert body == b"xxxxx", body
        assert 0.7 < took < 3, took  # and then the silence ended it, about _IDLE later
        await asyncio.wait_for(closed.wait(), 3)  # the upstream side was closed too
        w.close()

    with mock.patch.object(p, "_IDLE", 0.3), contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(main())


def test_stats():
    from agvisor.sandbox import _bytes

    assert [_bytes(t) for t in ("0B", "512KiB", "20.6MiB", "2GiB", "1.5TiB")] == [0, 512 * 1024, round(20.6 * 2**20), 2 * 2**30, round(1.5 * 2**40)]
    row = ('{"BlockIO":"0B / 0B","CPUPerc":"100.97%","Container":"agvisor-f3de3e82","ID":"0a379debb8a6","MemPerc":"11.40%",'
           '"MemUsage":"233.5MiB / 2GiB","Name":"agvisor-f3de3e82","NetIO":"1.11kB / 210B","PIDs":"52"}')  # as docker 29 printed it on runsc
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        if "gone" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Error response from daemon: No such container: gone\n")
        if "broken" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Cannot connect to the Docker daemon")
        return subprocess.CompletedProcess(cmd, 0, row + "\n", "")

    sb = Sandbox()
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        assert sb.stats() == {"cpu": 100.97, "memory": round(233.5 * 2**20), "memory_limit": 2 * 2**30, "pids": 52}
        assert calls[-1] == ["docker", "stats", "--no-stream", "--format", "{{json .}}", sb.name]
        for name, exc in (("gone", SandboxGone), ("broken", RuntimeError)):
            sb.name = name
            try:
                sb.stats()
            except exc as e:
                assert type(e) is exc, e  # a gone sandbox is not just any docker error
            else:
                raise AssertionError(f"{name}: expected {exc}")


def test_mcp_stats():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl, limits = 600, Limits(cpus=2, pids=512)

        def stats(self):
            return {"cpu": 101.4, "memory": 243 * 2**20, "memory_limit": 2048 * 2**20, "pids": 74}

        def stop(self):
            pass

    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: Fake()):
        assert m.stats() == "cpu 101% (limit 200%); memory 243 of 2048 MiB; processes 74 (limit 512)"
    m.reset()


def test_ports():
    a = Sandbox(ports=[8000, 3000]).run_args()
    assert "--publish=127.0.0.1::8000" in a and "--publish=127.0.0.1::3000" in a  # loopback only, a random host port
    assert a.index("--publish=127.0.0.1::8000") < a.index("ubuntu:24.04")  # a docker flag only works before the image
    assert not any(x.startswith("--publish") for x in Sandbox().run_args())  # nothing is published unless asked
    assert "--publish=127.0.0.1::80" in Sandbox(ports=[80], network="isolated").run_args()
    for bad in ([], [0], [65536], [True], ["80"], [80.0], [80, 80]):
        try:
            Sandbox(ports=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted ports={bad!r}")
    for bad in ({"gui": True}, {"network": "none"}, {"egress": ["x.org"]}, {"inject": {"x.com": {"A": "b"}}}):
        try:
            Sandbox(ports=[80], **bad)  # no route in from the host: docker would drop the port silently or refuse
        except ValueError:
            continue
        raise AssertionError(f"accepted ports with {bad!r}")
    sb = Sandbox(ports=[8000])
    calls, run = fake_docker()
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        assert sb.port(8000) == 49153 and calls[-1] == ["docker", "port", sb.name, "8000/tcp"]
        assert sb.port("9000") == 49153 and calls[-1][-1] == "9000/tcp"  # a number in a string is fine
    calls, run = fake_docker(fail=("8000/tcp",))  # never published: docker says so
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            sb.port(8000)
        except RuntimeError as e:
            assert str(e) == "boom"
        else:
            raise AssertionError("expected docker's error")


def test_mcp_ports():
    from agvisor import mcp_server as m

    def env(**kw):
        return mock.patch.dict(os.environ, {f"AGVISOR_{k}": v for k, v in kw.items()})

    assert m._ports() is None
    with env(PORTS="8000,3000", NETWORK="bridge"):
        assert m._ports() == [8000, 3000]
    for bad in ({"PORTS": "8000"},  # AGVISOR_NETWORK defaults to "none": no way in
                {"PORTS": "8000", "NETWORK": "bridge", "EGRESS": "pypi.org"}, {"PORTS": "8000", "NETWORK": "bridge", "INJECT": "{}"},
                {"PORTS": "web", "NETWORK": "bridge"}, {"PORTS": "0", "NETWORK": "bridge"}, {"PORTS": "80,", "NETWORK": "bridge"}):
        with env(**bad):
            try:
                m._ports()
            except SystemExit as e:
                assert "AGVISOR_PORTS" in str(e)
            else:
                raise AssertionError(f"accepted {bad!r}")

    class Fake:
        idle_ttl, host = 600, None

        def port(self, p):
            if p != 8000:
                raise RuntimeError(f"No public port '{p}/tcp' published for agvisor-x")
            return 49153

        def stop(self):
            pass

    made = {}

    def create(*a, **kw):
        made.update(kw)
        return Fake()

    m._sb = None
    with mock.patch.object(m, "create_sandbox", create), env(PORTS="8000", NETWORK="bridge"):
        assert m.port(8000) == "127.0.0.1:49153" and made["ports"] == [8000] and made["network"] == "bridge"
        assert m.port(9) == "No public port '9/tcp' published for agvisor-x"  # the agent is told, not given a traceback
        m._sb.host = "ssh://u@vm"
        assert m.port(8000) == "127.0.0.1:49153 on ssh://u@vm"  # that machine's loopback, not the agent's host
    m.reset()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", create):
        m.port(8000)
        assert made["ports"] is None  # not asked for: nothing published
    m.reset()


def test_env():
    secret = "s3cret-value"
    for bad in ({}, {"1A": "x"}, {"A B": "x"}, {"A=B": "x"}, {"": "x"}, {"A": f"{secret}\nB=evil"}, {"A": f"{secret}\r"},
                {"A": f"{secret}\0"}, {"A": 1}, {"A": None}, ["A=b"], "A=b"):
        try:
            Sandbox(env=bad)
        except ValueError as e:
            assert secret not in str(e), "an error must not repeat a value: it may be a secret"
            continue
        raise AssertionError(f"accepted {bad!r}")

    assert "--env-file=/dev/stdin" not in Sandbox().run_args()
    a = Sandbox(env={"MODE": "ci"}).run_args()
    assert "--env-file=/dev/stdin" in a and a.index("--env-file=/dev/stdin") < a.index("ubuntu:24.04")  # a flag, before the image
    assert not any("MODE" in x for x in a)  # stdin only: a command line shows up in `ps`

    # create_sandbox: the variables ride on docker run's stdin; with egress= the proxy's own lines come last and win
    calls = []

    def run(cmd, **kw):
        calls.append((cmd[1:], kw.get("input")))
        return subprocess.CompletedProcess(cmd, 0, "172.18.0.2\n" if cmd[1] == "inspect" else "", "")

    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(env={"MODE": "ci", "TOKEN": secret}).stop()
        assert calls[0][1] == f"MODE=ci\nTOKEN={secret}\n" and not any(secret in str(c) for c, _ in calls)
        calls.clear()
        create_sandbox(env={"HTTPS_PROXY": "http://evil:1"}, egress=["pypi.org"]).stop()
        box = [i for c, i in calls if c[0] == "run" and "ubuntu:24.04" in c][0]
        lines = box.splitlines()
        assert lines[0] == "HTTPS_PROXY=http://evil:1" and [l for l in lines if l.startswith("HTTPS_PROXY=")][-1].startswith("HTTPS_PROXY=http://agvisor:")
        calls.clear()
        create_sandbox().stop()
        assert calls[0][1] is None  # nothing to say on stdin

    # exec(env=, cwd=): a file only we can read, never `-e`; removed afterwards
    seen = []

    def docker(cmd, **kw):
        flag = next(x for x in cmd if x.startswith("--env-file="))
        path = flag.split("=", 1)[1]
        seen.append((cmd, Path(path).read_text(), oct(os.stat(path).st_mode & 0o777), path))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    sb = Sandbox()
    with mock.patch("agvisor.sandbox.subprocess.run", docker):
        sb.exec("true", env={"API_KEY": secret, "N": "1"}, cwd="/workspace/proj")
    cmd, text, mode, path = seen[0]
    assert text == f"API_KEY={secret}\nN=1\n" and mode == "0o600" and not os.path.exists(path)
    assert not any(secret in x for x in cmd) and "-e" not in cmd  # `ps` on the client's machine shows the command line
    assert cmd[cmd.index("-w") + 1] == "/workspace/proj" and cmd.index("-w") < cmd.index(sb.name)  # flags before the container

    streamed = []
    sb._stream = lambda *args, on_output: (streamed.append(args), subprocess.CompletedProcess([], 0, "", ""))[1]
    sb.exec("true", env={"A": "b"}, on_output=lambda _: None)  # the streaming path takes them too
    assert any(x.startswith("--env-file=") for x in streamed[0]) and streamed[0][:2] == ("exec", "-i")
    for bad in ({"cwd": "relative"}, {"cwd": ""}, {"cwd": "/a\0b"}, {"env": {"A B": "c"}}, {"env": {"A": "b\nc"}}):
        try:
            sb.exec("true", **bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    plain = []
    with mock.patch("agvisor.sandbox.subprocess.run", lambda cmd, **kw: (plain.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]):
        sb.exec("true", cwd="/tmp")  # cwd alone needs no file
        sb.exec("true")
    assert "-w" in plain[0] and not any(x.startswith("--env-file") for c in plain for x in c) and "-w" not in plain[1]

    # spawn(): the job starts with them
    jobs = []
    sb2 = Sandbox()
    sb2.exec = lambda cmd, **kw: (jobs.append(kw), subprocess.CompletedProcess([], 0, "", ""))[1]
    sb2.spawn("x", env={"A": "b"}, cwd="/tmp")
    assert jobs[0]["env"] == {"A": "b"} and jobs[0]["cwd"] == "/tmp"


def test_env_cli_and_mcp():
    from agvisor import cli, mcp_server as m

    def main(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    # agvisor exec: --env NAME=VALUE, --env NAME (from our own environment), --cwd
    seen = []

    class Fake(Sandbox):
        def exec(self, cmd, **kw):
            seen.append((cmd, kw["env"], kw["cwd"]))
            return subprocess.CompletedProcess([], 0, "", "")

    with mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Fake(name="old"))]), mock.patch.dict(os.environ, {"TOK": "s3"}):
        assert main("exec", "agvisor-old", "--env", "A=1", "--env", "TOK", "--cwd", "/tmp", "echo", "hi")[0] == 0
        assert seen[-1] == ("echo hi", {"A": "1", "TOK": "s3"}, "/tmp")
        assert main("exec", "agvisor-old", "true")[0] == 0 and seen[-1] == ("true", None, None)
        assert main("exec", "agvisor-old", "--env", "NOPE", "true")[0] == 2  # unset: a usage error, not a variable set to nothing
    with mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Sandbox(name="old"))]):  # the real exec(): it validates
        rc, _, err = main("exec", "agvisor-old", "--env", "A B=secret-x", "true")
        assert rc == 2 and "A B" in err and "secret-x" not in err  # a bad name is refused without echoing the value

    # agvisor run --env reaches create_sandbox
    made = {}

    class Box:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec(self, cmd, timeout, on_output):
            return subprocess.CompletedProcess([], 0, "", "")

    def create(image, **kw):
        made.update(kw)
        return Box()

    with mock.patch("agvisor.cli.create_sandbox", create), mock.patch.dict(os.environ, {"TOK": "s3"}):
        assert main("run", "img", "--env", "MODE=ci", "--env", "TOK", "--", "true")[0] == 0
        assert made["env"] == {"MODE": "ci", "TOK": "s3"}
        assert main("run", "img", "--", "true")[0] == 0 and made["env"] is None

    # MCP: AGVISOR_ENV is the user's, validated at startup, and reaches create_sandbox
    def envset(**kw):
        return mock.patch.dict(os.environ, {f"AGVISOR_{k}": v for k, v in kw.items()})

    assert m._envvars() is None
    with envset(ENV='{"MODE": "ci"}'):
        assert m._envvars() == {"MODE": "ci"}
    for bad in ("{", "[]", '{"A B": "x"}', '{"A": "line\\nbreak-s3cret"}', '{"A": 1}', "{}"):
        with envset(ENV=bad):
            try:
                m._envvars()
            except SystemExit as e:
                assert "AGVISOR_ENV" in str(e) and "s3cret" not in str(e)
            else:
                raise AssertionError(f"accepted {bad!r}")

    class Gone:
        idle_ttl, host = 30, None

        def exec(self, cmd, *, timeout, on_output):
            return subprocess.CompletedProcess([], 0, "", "")

        def stop(self):
            pass

    kws = []
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: (kws.append(kw), Gone())[1]), envset(ENV='{"MODE": "ci"}'):
        m.run("x")
        assert kws[0]["env"] == {"MODE": "ci"}
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: (kws.append(kw), Gone())[1]):
        m.run("x")
        assert kws[1]["env"] is None
    m._sb = None


def test_pause_resume():
    import asyncio

    from agvisor import SandboxPaused, aio, cli

    calls, out = [], {"rc": 0, "err": ""}

    def run(cmd, **kw):
        calls.append(cmd[1:])
        return subprocess.CompletedProcess(cmd, out["rc"], "", out["err"])

    sb = Sandbox(name="proj")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb.pause()
        assert calls == [["pause", "agvisor-proj"]]
        calls.clear()
        sb.resume()
        assert calls == [["unpause", "agvisor-proj"]]  # no idle_ttl: no clock to restart
        calls.clear()
        Sandbox(name="proj", idle_ttl=60).resume()  # the idle clock restarts: the wall clock ran while the sandbox was frozen
        assert calls[0] == ["unpause", "agvisor-proj"] and calls[1][:3] == ["exec", "agvisor-proj", "touch"], calls
        out.update(rc=1, err="Error response from daemon: container agvisor-proj is already paused")
        try:
            sb.pause()
        except RuntimeError as e:
            assert "already paused" in str(e)  # docker's words, not a silent no-op
        else:
            raise AssertionError("pausing a paused sandbox must raise")
        out.update(rc=1, err="Error response from daemon: container agvisor-proj is not paused")
        try:
            sb.resume()
        except RuntimeError as e:
            assert "not paused" in str(e)
        else:
            raise AssertionError("resuming a running sandbox must raise")

        # exec() on a paused sandbox: SandboxPaused, not a command that "failed" with exit code 1
        out.update(rc=1, err="Error response from daemon: Container 1f2e3d is paused, unpause the container before exec")
        try:
            sb.exec("true")
        except SandboxPaused as e:
            assert "resume()" in str(e) and isinstance(e, RuntimeError) and not isinstance(e, SandboxGone)
        else:
            raise AssertionError("exec() in a paused sandbox must raise SandboxPaused")
        out.update(rc=1, err="the queue is paused")  # a command that says so itself is just a failed command
        assert sb.exec("false").returncode == 1
        out.update(rc=2, err="Error response from daemon: Container x is paused")  # only docker's exit code 1
        assert sb.exec("false").returncode == 2

    # stop() of a paused sandbox thaws it first (docker can't remove what is frozen); a running one is just removed
    out.update(rc=0, err="")
    box = Sandbox(name="proj")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        box.pause()
        assert box._paused
        calls.clear()
        box.stop()
        assert calls == [["unpause", "agvisor-proj"], ["rm", "-f", "agvisor-proj"]] and not box._paused
        calls.clear()
        box.stop()
        assert calls == [["rm", "-f", "agvisor-proj"]]  # once is enough
        box.pause()
        box.resume()
        calls.clear()
        box.stop()
        assert calls == [["rm", "-f", "agvisor-proj"]] and not box._paused  # resumed: nothing to thaw
        calls.clear()
        listed = Sandbox(name="proj")
        listed._paused = True  # what list_sandboxes() sets for a paused container
        listed.stop()
        assert calls == [["unpause", "agvisor-proj"], ["rm", "-f", "agvisor-proj"]]

    # snapshot still unpauses what it paused, through the same helper
    calls.clear()
    out.update(rc=0, err="")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        Sandbox(name="proj", idle_ttl=60).snapshot("t")
    verbs = [c[0] for c in calls]
    assert verbs.index("pause") < verbs.index("unpause"), verbs

    # list_sandboxes: a paused container is running, and paused; `ls` says so
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    ps = f"agvisor-a\tpaused\ta\t\t{now}\t\nagvisor-b\trunning\tb\t\t{now}\t\nagvisor-c\texited\tc\t\t{now}\t\n"
    calls2, fake = fake_docker(ps_out=ps)
    with mock.patch("agvisor.sandbox.subprocess.run", fake):
        boxes = list_sandboxes()
        assert [(b._running, b._paused) for b in boxes] == [(True, True), (True, False), (False, False)]
        assert not Sandbox()._paused

        def main(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                try:
                    rc = cli.main(list(argv))
                except SystemExit as e:
                    rc = e.code
            return rc, buf.getvalue()

        rc, text = main("ls")
        assert [l.split("\t")[:2] for l in text.splitlines()] == [["agvisor-a", "paused"], ["agvisor-b", "running"], ["agvisor-c", "stopped"]]
        calls2.clear()
        assert main("pause", "agvisor-b") == (0, "done\n") and ["docker", "pause", "agvisor-b"] in calls2
        calls2.clear()
        assert main("resume", "agvisor-a") == (0, "done\n") and ["docker", "unpause", "agvisor-a"] in calls2
        assert main("pause", "agvisor-c")[0] == 2  # not running
        assert main("pause", "nope")[0] == 2 and main("pause")[0] == 2

    # the async interface awaits them
    seen = []

    async def scenario():
        box = aio.AsyncSandbox(name="proj")
        with mock.patch("agvisor.sandbox.subprocess.run", lambda cmd, **kw: (seen.append(cmd[1]), subprocess.CompletedProcess(cmd, 0, "", ""))[1]):
            await box.pause()
            await box.resume()

    asyncio.run(scenario())
    assert seen == ["pause", "unpause"]


def simulate_pid1(script, freeze_at=0, hook_at=0, hook="", files=None):
    """Run PID 1's shell script against a simulated clock: `date`, `sleep`, `stat` and `touch` are shims, so 100 s pass in
    milliseconds. The n-th `sleep` can take 1000 s instead of its own (freeze_at: a pause()), and before the hook_at-th one
    the shell command `hook` runs ($D is the directory, clock in $D/clock; what renew() or the sandbox's own code might do).
    The script's /tmp/.agvisor_* files live in that directory. Returns (exit code, sleeps, directory contents reader)."""
    with tempfile.TemporaryDirectory() as d:
        for name, body in {
            "date": 'cat "$D/clock"',  # only `date +%s` is used
            "stat": 'cat "$D/mtime"',  # only `stat -c %Y FILE`
            "touch": 'cp "$D/clock" "$D/mtime"',
            "sleep": 'n=$(( $(cat "$D/n") + 1 )); echo $n > "$D/n"; [ $n -gt 300 ] && kill -TERM $PPID\n'  # a runaway loop is killed
                     '[ $n -eq "$HOOK_N" ] && eval "$HOOK"\n'
                     'c=$(cat "$D/clock"); if [ $n -eq "$FREEZE" ]; then echo $((c + 1000)) > "$D/clock"; else echo $((c + $1)) > "$D/clock"; fi',
        }.items():
            Path(d, name).write_text(f"#!/bin/sh\n{body}\n")
            os.chmod(Path(d, name), 0o755)
        for name in ("clock", "mtime", "n"):
            Path(d, name).write_text("0\n")
        env = {"PATH": f"{d}:/usr/bin:/bin", "D": d, "FREEZE": str(freeze_at), "HOOK_N": str(hook_at), "HOOK": hook}
        r = subprocess.run(["sh", "-c", script.replace("/tmp/.agvisor", f"{d}/.agvisor")], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, int(Path(d, "n").read_text()), lambda name: Path(d, name).read_text().strip()


def test_idle_watchdog_survives_a_freeze():
    """The idle watchdog (PID 1 of an idle_ttl sandbox) against a simulated clock: a `sleep` that took 1000 s was a
    pause(), not idleness, and must not expire the sandbox when it wakes."""
    script = Sandbox(idle_ttl=100).run_args()[-1]
    rc, sleeps, _ = simulate_pid1(script)  # never frozen: expires once 100 s have passed without activity
    assert rc == 0 and sleeps == 20, (rc, sleeps)  # 20 sleeps of 5 s reach 100 s
    rc, sleeps, _ = simulate_pid1(script, freeze_at=3)  # frozen during the third sleep: it goes on, and expires a full ttl after waking
    assert rc == 0 and sleeps == 3 + 20, (rc, sleeps)  # without the touch it would have expired right after sleep 3


def test_renewable_lifetime():
    a = Sandbox(lifetime=60, renewable=True).run_args()
    assert "--rm" in a and a[-3:-1] == ["sh", "-c"] and "/tmp/.agvisor_deadline" in a[-1]  # PID 1 watches a deadline file
    assert Sandbox(lifetime=60).run_args()[-2:] == ["sleep", "60"]  # not renewable: unchanged, a hard cap nothing inside can touch
    assert "stat -c" not in a[-1] and "stat -c" in Sandbox(lifetime=60, idle_ttl=30, renewable=True).run_args()[-1]  # idle clock only if asked
    for bad in ({"renewable": True}, {"renewable": True, "idle_ttl": 30}, {"renewable": True, "lifetime": 60, "gui": True}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")

    # PID 1, simulated. 5 s steps, so a deadline at 100 s takes 20 sleeps
    script = Sandbox(lifetime=100, renewable=True).run_args()[-1]
    rc, sleeps, _ = simulate_pid1(script)
    assert rc == 0 and sleeps == 20, (rc, sleeps)
    later = "echo $(( $(cat $D/clock) + 200 )) > $D/.agvisor_deadline"  # renew(200) before the 3rd sleep, when it is 10 s in
    rc, sleeps, _ = simulate_pid1(script, hook_at=3, hook=later)
    assert rc == 0 and sleeps == 42, (rc, sleeps)  # 210 s / 5
    sooner = "echo $(( $(cat $D/clock) + 7 )) > $D/.agvisor_deadline"  # renew(7) at 5 s in: the deadline is 12 s
    rc, sleeps, _ = simulate_pid1(script, hook_at=2, hook=sooner)
    assert rc == 0 and sleeps == 3, (rc, sleeps)  # sleeps to 5 s and 10 s, then one of 2 s: never past the deadline
    rc, sleeps, _ = simulate_pid1(script, hook_at=2, hook="rm $D/.agvisor_deadline")  # no deadline: expired, not forever
    assert rc == 0 and sleeps == 2, (rc, sleeps)
    rc, sleeps, _ = simulate_pid1(script, hook_at=2, hook="echo never > $D/.agvisor_deadline")  # junk reads as 0: expired
    assert rc == 0 and sleeps == 2, (rc, sleeps)
    # the lifetime is a wall-clock deadline: 1000 s frozen by pause() are 1000 s of it
    rc, sleeps, _ = simulate_pid1(Sandbox(lifetime=500, renewable=True).run_args()[-1], freeze_at=3)
    assert rc == 0 and sleeps == 3, (rc, sleeps)

    # with idle_ttl too: whichever comes first, and a freeze is not idleness
    both = Sandbox(lifetime=1000, idle_ttl=100, renewable=True).run_args()[-1]
    rc, sleeps, _ = simulate_pid1(both)
    assert rc == 0 and sleeps == 20, (rc, sleeps)  # idle after 100 s
    rc, sleeps, _ = simulate_pid1(both, hook_at=10, hook="cp $D/clock $D/mtime")  # an exec at 45 s restarts the idle clock
    assert rc == 0 and sleeps == 29, (rc, sleeps)  # 5k - 45 >= 100
    rc, sleeps, _ = simulate_pid1(Sandbox(lifetime=50, idle_ttl=1000, renewable=True).run_args()[-1])
    assert rc == 0 and sleeps == 10, (rc, sleeps)  # the lifetime came first
    rc, sleeps, _ = simulate_pid1(Sandbox(lifetime=5000, idle_ttl=100, renewable=True).run_args()[-1], freeze_at=3)
    assert rc == 0 and sleeps == 3 + 20, (rc, sleeps)  # frozen 1000 s during sleep 3: still a full idle_ttl after waking

    # renew(): the command it runs, in a real shell against the same clock
    cmds = []
    sb = Sandbox(lifetime=100, renewable=True)
    sb.exec = lambda cmd, **kw: (cmds.append((cmd, kw)), subprocess.CompletedProcess([], answer[0], "", "boom"))[1]
    answer = [0]
    sb.renew(300)
    cmd, kw = cmds[0]
    assert kw["timeout"] == 30 and "/tmp/.agvisor_deadline" in cmd and "+ 300" in cmd and "mv " in cmd  # written to a file and moved: atomic
    for bad in (0, -5, 1.5, "60", None, True):
        try:
            sb.renew(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert len(cmds) == 1  # refused before any docker call
    answer[0] = 3
    try:
        sb.renew(60)
    except ValueError as e:
        assert "not renewable" in str(e)
    else:
        raise AssertionError("renew() on a sandbox without a deadline file must raise")
    answer[0] = 1
    try:
        sb.renew(60)
    except RuntimeError as e:
        assert "renew failed" in str(e) and not isinstance(e, ValueError)
    else:
        raise AssertionError("a failing renew must raise")

    with tempfile.TemporaryDirectory() as d:
        shim = Path(d, "date")
        shim.write_text("#!/bin/sh\necho 1000\n")
        os.chmod(shim, 0o755)
        cmd = cmd.replace("/tmp/.agvisor", f"{d}/.agvisor")
        run = lambda: subprocess.run(["sh", "-c", cmd], env={"PATH": f"{d}:/usr/bin:/bin"}, capture_output=True, text=True)
        assert run().returncode == 3  # no deadline file: not a renewable sandbox
        Path(d, ".agvisor_deadline").write_text("1100\n")
        assert run().returncode == 0 and Path(d, ".agvisor_deadline").read_text().strip() == "1300"  # now + 300, not the old one + 300
        assert not Path(d, ".agvisor_deadline.new").exists()

    # idle_ttl: renew() counts as activity, like any exec(); and it works from a handle that list_sandboxes() made
    idle = Sandbox(lifetime=100, idle_ttl=30, renewable=True)
    seen = []
    idle._docker = lambda *args, **kw: (seen.append(args), subprocess.CompletedProcess(args, 0, "", ""))[1]
    idle.renew(60)
    assert "/tmp/.agvisor_active" in seen[0][-1] and seen[0][-1].count("+ 60") == 1  # the exec's trap touches the marker
    handle = Sandbox(host=None)  # a handle knows nothing of renewable: the file is the truth
    handle._docker = lambda *args, **kw: subprocess.CompletedProcess(args, 0, "", "")
    handle.renew(5)


def test_renew_cli():
    import asyncio

    from agvisor import aio, cli

    seen = []

    class Fake(Sandbox):
        def renew(self, seconds):
            seen.append(seconds)
            if self.name == "agvisor-plain":
                raise ValueError("agvisor-plain is not renewable: create it with lifetime=... and renewable=True")

    def main(*argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    with mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Fake(name="a")), _running(Fake(name="plain"))]):
        assert main("renew", "agvisor-a", "600")[:2] == (0, "done\n") and seen == [600]
        rc, _, err = main("renew", "agvisor-plain", "60")
        assert rc == 2 and "not renewable" in err  # a usage error, not a traceback
        assert main("renew", "agvisor-a")[0] == 2 and main("renew", "agvisor-a", "soon")[0] == 2 and main("renew", "nope", "5")[0] == 2

    box = Sandbox(name="a")  # aio holds the class's own renew(): what can be faked is what it calls
    box.exec = lambda cmd, **kw: (seen.append(cmd), subprocess.CompletedProcess([], 0, "", ""))[1]

    async def scenario():
        await aio.AsyncSandbox.of(box).renew(30)

    asyncio.run(scenario())
    assert "+ 30 )" in seen[-1]


def test_python_contexts():
    import sys

    from agvisor import mcp_server as m
    from agvisor.sandbox import _py

    assert " 5 2>&1" in _py(5, 100) and " 5 a 2>&1" in _py(5, 100, "a") and "kill my-ctx 2>&1" in _py("kill", 100, "my-ctx")

    d = tempfile.mkdtemp(dir="/tmp")  # short: a unix socket path is limited to ~100 characters
    env = {**os.environ, "AGVISOR_PYDIR": f"{d}/py", "PATH": f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"}
    ran = []

    def local(cmd, *, input=None, timeout):  # the real command, run here with sh instead of inside a sandbox
        ran.append(cmd)
        try:
            return subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], 124, "", "")

    sb = Sandbox()
    sb.exec = local
    try:
        assert "new python session" in sb.python("x = 'default'").stdout
        assert "new python session" in sb.python("x = 'a'", context="a").stdout  # its own interpreter: told so
        assert "new python session" in sb.python("x = 'b'", context="b").stdout
        # three interpreters, three sets of globals
        assert [sb.python("x", context=c).stdout for c in (None, "a", "b")] == ["'default'\n", "'a'\n", "'b'\n"]
        assert sb.python("x", context="a").stdout == "'a'\n"  # and context a is the same interpreter as before
        sb.python("only_in_a = 1", context="a")
        assert "NameError" in sb.python("only_in_a", context="b").stdout and "NameError" in sb.python("only_in_a").stdout
        assert sorted(os.listdir(d)) == ["py", "py-a", "py-b"], os.listdir(d)  # a directory per context: sockets can't collide

        # restart and a stuck call touch only their own context
        assert "new python session" in sb.python("x", context="a", restart=True).stdout and "NameError" in sb.python("only_in_a", context="a").stdout
        assert sb.python("x", context="b").stdout == "'b'\n" and sb.python("x").stdout == "'default'\n"
        assert "TimeoutError" in sb.python("while True: pass", timeout=1, context="b").stdout  # the alarm: variables stay
        assert sb.python("x", context="b").stdout == "'b'\n"
        r = sb.python("import signal; signal.signal(signal.SIGALRM, signal.SIG_IGN)\nwhile True: pass", timeout=1, context="b")
        assert r.returncode == 124  # stuck where the alarm can't reach: only b's interpreter was killed
        assert sb.python("x", context="b").stdout.startswith("[new python session") and sb.python("x").stdout == "'default'\n"
        sb.python("x = 'a again'", context="a")  # a was restarted above, and is untouched by what happened to b since
        assert sb.python("x", context="a").stdout == "'a again'\n" and "NameError" in sb.python("x", context="b").stdout

        n = len(ran)
        for bad in ("../x", "", "a b", "a;b", "a/b", "a.b", "-a", "x" * 33, "$(id)", "é", 5, b"a"):
            try:
                sb.python("1", context=bad)
            except ValueError as e:
                assert "invalid python context" in str(e)
                continue
            raise AssertionError(f"accepted context {bad!r}")
        assert len(ran) == n  # refused before anything ran
        assert sb.python("1", context="x" * 32).stdout.startswith("[new python session")  # the longest name
    finally:
        for c in (None, "a", "b", "x" * 32):
            subprocess.run(["/bin/sh", "-c", _py("kill", 1000, c)], env=env, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)

    # the MCP tool: `context` is passed on; "" is no context; a bad name is an answer, not a traceback
    class Fake:
        idle_ttl = 60

        def __init__(self):
            self.calls = []

        def python(self, code, *, timeout, restart, context=None):
            if context is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", context):
                raise ValueError(f"invalid python context {context!r}: letters, digits, '_', '-' only (max 32)")
            self.calls.append(context)
            return subprocess.CompletedProcess([], 0, "ok\n", "")

        def stop(self):
            pass

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.python("x") == "ok\n" and m.python("x", context="a") == "ok\n" and m.python("x", context="") == "ok\n"
        assert box.calls == [None, "a", None]
        msg = m.python("x", context="a b")
        assert msg.startswith("invalid python context") and len(box.calls) == 3
    m._sb = None


def test_mounts():
    from agvisor import cli, mcp_server as m

    ok = lambda mounts: Sandbox(mounts=mounts).run_args()
    a = ok({"/data/models": "/models", "/srv/ref/": "/ref"})
    assert "--mount=type=bind,source=/data/models,target=/models,readonly" in a  # read-only, always: there is no way to ask for more
    assert "--mount=type=bind,source=/srv/ref,target=/ref,readonly" in a  # normalised
    assert a.index("--mount=type=bind,source=/srv/ref,target=/ref,readonly") < a.index("ubuntu:24.04")  # a flag, before the image
    assert not any(x.startswith("--mount") or x.startswith("--volume=/") for x in Sandbox().run_args())
    assert all(x.endswith(",readonly") for x in a if x.startswith("--mount"))
    a = ok({"/x//y/./z": "/a//b"})
    assert "--mount=type=bind,source=/x/y/z,target=/a/b,readonly" in a
    assert "--volume=agvisor-p:/workspace" in Sandbox(name="p", mounts={"/d": "/d"}).run_args()  # next to a named sandbox's volume

    for bad in ({}, [], "/a:/b", {"/": "/x"}, {"/x": "/"}, {"relative": "/x"}, {"/x": "relative"}, {"": "/x"}, {"/a/../etc": "/x"},
                {"/a": "/x/../y"}, {"/proc": "/x"}, {"/proc/1": "/x"}, {"/sys/kernel": "/x"}, {"/dev": "/x"}, {"/dev/shm": "/x"},
                {"/run": "/x"}, {"/var/run/docker.sock": "/x"}, {"/var/run": "/x"}, {"/home/me/docker.sock": "/x"}, {"/a": "/proc/x"},
                {"/a": "/sys"}, {"/a": "/dev/x"}, {"//": "/x"}, {"//a": "/x"}, {"/a": "//b"},
                {"/a,readonly=false": "/x"}, {"/a": "/x,type=volume"}, {"/a\nb": "/x"}, {"/a": 'x"'}, {"/a\0": "/x"}, {"/a": 5}, {5: "/x"},
                {"/a": "/x", "/b": "/x/"}, {"/a": "/x", "/b": "/x//"}):
        try:
            Sandbox(mounts=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    ok({"/proc-data": "/x"})  # a prefix of a name, not a path under it
    ok({"/var/running": "/x"})
    ok({"/a": "/devices"})
    ok({"/a": "/etc/data"})

    # a mount is a docker run argument like any other: create_sandbox passes it on
    calls = []
    with mock.patch("agvisor.sandbox.subprocess.run", lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]):
        create_sandbox(mounts={"/data": "/data"}).stop()
    assert "--mount=type=bind,source=/data,target=/data,readonly" in calls[0]

    # MCP: AGVISOR_MOUNTS is the user's, validated at startup
    def envset(**kw):
        return mock.patch.dict(os.environ, {f"AGVISOR_{k}": v for k, v in kw.items()})

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as e:
        assert m._mounts() is None
        with envset(MOUNTS=f"{d}:/models, {e}:/ref"):
            assert m._mounts() == {d: "/models", e: "/ref"}
        for bad in (f"{d}", f"{d}:", ":/x", f"{d}:rel", f"{d}:/x,{d}:/y", f"{d}:/x,{e}:/x", "/:/x", f"{d}:/proc/x", "/var/run/docker.sock:/x", f"{d}:/x,"):
            with envset(MOUNTS=bad):
                try:
                    m._mounts()
                except SystemExit as err:
                    assert "AGVISOR_MOUNTS" in str(err), bad
                else:
                    raise AssertionError(f"accepted {bad!r}")
        with envset(MOUNTS=f"{d}/nope:/x"):  # docker is local: a missing path is reported now, not on the agent's first call
            try:
                m._mounts()
            except SystemExit as err:
                assert "does not exist" in str(err)
            else:
                raise AssertionError("a missing path was accepted")
        with envset(MOUNTS=f"{d}/nope:/x", HOST="ssh://u@vm"):  # on another machine: can't tell from here
            assert m._mounts() == {f"{d}/nope": "/x"}

        class Box:
            idle_ttl, host = 30, None

            def exec(self, cmd, *, timeout, on_output):
                return subprocess.CompletedProcess([], 0, "", "")

            def stop(self):
                pass

        kws = []
        m._sb = None
        with mock.patch.object(m, "create_sandbox", lambda *a, **kw: (kws.append(kw), Box())[1]), envset(MOUNTS=f"{d}:/models"):
            m.run("x")
            assert kws[0]["mounts"] == {d: "/models"}
        m._sb = None
        with mock.patch.object(m, "create_sandbox", lambda *a, **kw: (kws.append(kw), Box())[1]):
            m.run("x")
            assert kws[1]["mounts"] is None
        m._sb = None

        # agvisor run --mount
        made = {}

        class Run:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def exec(self, cmd, timeout, on_output):
                return subprocess.CompletedProcess([], 0, "", "")

        def main(*argv):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    return cli.main(list(argv))
                except SystemExit as err:
                    return err.code

        with mock.patch("agvisor.cli.create_sandbox", lambda image, **kw: (made.update(kw), Run())[1]):
            assert main("run", "img", "--mount", f"{d}:/models", "--mount", f"{e}:/ref", "--", "true") == 0
            assert made["mounts"] == {d: "/models", e: "/ref"}
            assert main("run", "img", "--", "true") == 0 and made["mounts"] is None
            assert main("run", "img", "--mount", "nodest", "--", "true") == 2 and main("run", "img", "--mount", ":/x", "--", "true") == 2


def test_egress_policy():
    import signal

    from agvisor import egress_proxy as p

    allow, deny = p.parse_policy(["pypi.org", "*.x.org:8080", "!admin.x.org", "!10.0.0.0/8", "!bad.org:25", "PyPI.io", "!Mixed.Case.org"])
    assert [r[1] for r in allow] == ["pypi.org", "*.x.org", "pypi.io"]
    assert deny == [("name", "admin.x.org", None), ("net", p.parse_rules(["10.0.0.0/8"])[0][1], None), ("name", "bad.org", (25,)),
                    ("name", "mixed.case.org", None)]  # no port on a deny = every port; an explicit one is kept
    assert p.parse_policy([]) == ([], [])
    for bad in ("!", "!!x.org", "! x.org", "!a b", "!x.org:0", "!*", "!1.2.3.4/33", ""):
        try:
            p.parse_policy([bad])
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")

    allow, deny = p.parse_policy(["*.x.org:*", "x.org", "10.0.0.0/8:*", "!admin.x.org", "!10.0.0.5", "!*.internal.x.org:22"])
    ok = lambda h, port=443: p.check(allow, deny, h, port)
    assert ok("a.x.org") == "name" and ok("a.x.org", 9999) == "name" and ok("x.org") == "name"
    assert not ok("admin.x.org") and not ok("admin.x.org", 80) and not ok("admin.x.org", 9999)  # a deny is every port
    assert ok("a.admin.x.org") == "name"  # exact name: its subdomains are not named
    assert ok("10.0.0.4", 8080) == "net" and not ok("10.0.0.5", 8080) and not ok("10.0.0.5")
    assert not ok("db.internal.x.org", 22) and ok("db.internal.x.org", 443) == "name"  # only the denied port
    assert not ok("evil.org")  # no allow rule: still refused
    assert p.check(allow, [], "admin.x.org", 443) == "name"  # without the deny it would pass

    # edit(): the file changes in one step and the proxy is signalled; a bad entry changes nothing and signals nothing
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rules")
        Path(path).write_text("pypi.org\n*.pythonhosted.org\n!admin.pypi.org\n")
        hups = []
        old = signal.signal(signal.SIGHUP, lambda *_: hups.append(1))
        try:
            read = lambda: Path(path).read_text().split()
            p.edit([("allow", "Example.COM")], path, os.getpid())
            assert read() == ["pypi.org", "*.pythonhosted.org", "!admin.pypi.org", "example.com"] and len(hups) == 1
            p.edit([("deny", "example.com"), ("allow", "admin.pypi.org")], path, os.getpid())  # the last word on an entry stands
            assert read() == ["pypi.org", "*.pythonhosted.org", "!example.com", "admin.pypi.org"] and len(hups) == 2
            p.edit([("allow", "example.com"), ("deny", "example.com")], path, os.getpid())  # twice in one go
            assert read()[-1] == "!example.com" and "example.com" not in read() and len(hups) == 3
            before = Path(path).read_text()
            for ops in ([("allow", "a b")], [("allow", "x.org:0")], [("allow", "*")], [("frob", "x.org")], [("allow", "ok.org"), ("allow", "")]):
                try:
                    p.edit(ops, path, os.getpid())
                except ValueError:
                    continue
                raise AssertionError(f"accepted {ops!r}")
            assert Path(path).read_text() == before and len(hups) == 3 and not os.path.exists(path + ".new")  # nothing written, nobody told
            p.edit([("deny", "!x.org")], path, os.getpid())  # a leading '!' is the same as none
            assert read()[-1] == "!x.org" and "!!x.org" not in read()
        finally:
            signal.signal(signal.SIGHUP, old)


def test_egress_live_reload():
    """The real proxy process: edit() rewrites its rules and signals it. A new connection follows the new rules at once,
    and a tunnel that is already open is closed when its host is no longer allowed."""
    import asyncio
    import base64
    import signal
    import socket
    import sys

    import agvisor.egress_proxy as p

    async def echo(r, w):  # the "origin": echoes what a tunnel sends until it ends
        while data := await r.read(1024):
            w.write(data)
            await w.drain()
        w.close()

    async def connect(port, target, auth):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"CONNECT {target} HTTP/1.1\r\n".encode() + auth + b"\r\n")
        await w.drain()
        return r, w, await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)

    async def ping(port, target, auth):  # True if a tunnel to target works end to end
        r, w, head = await connect(port, target, auth)
        try:
            if not head.startswith(b"HTTP/1.1 200"):
                assert head.startswith(b"HTTP/1.1 403"), head
                return False
            w.write(b"ping\n")
            return await asyncio.wait_for(r.readline(), 5) == b"ping\n"
        finally:
            w.close()

    async def main(proc, rules, port, op):
        auth = b"Proxy-Authorization: Basic " + base64.b64encode(b"agvisor:tok") + b"\r\n"
        target = f"127.0.0.1:{op}"
        edit = lambda *ops: asyncio.to_thread(subprocess.run, [sys.executable, p.__file__, "edit"], input="\n".join(ops) + "\n", text=True,
                                              env={**os.environ, "AGVISOR_RULES": rules, "AGVISOR_PID": str(proc.pid)}, capture_output=True)
        for _ in range(100):  # the proxy takes a moment to start
            try:
                assert await ping(port, target, auth)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("the proxy never came up")
        assert Path(rules).read_text().split() == [target]  # written at start: what the host reads back

        r, w, head = await connect(port, target, auth)  # a tunnel that stays open
        assert head.startswith(b"HTTP/1.1 200")
        w.write(b"one\n")
        assert await asyncio.wait_for(r.readline(), 5) == b"one\n"

        res = await edit(f"deny {target}")
        assert res.returncode == 0, res.stderr
        assert await asyncio.wait_for(r.read(), 5) == b""  # the open tunnel was closed: dropEstablished
        w.close()
        assert not await ping(port, target, auth)  # a new one is refused...
        assert Path(rules).read_text().split() == [f"!{target}"]

        res = await edit(f"allow {target}")
        assert res.returncode == 0 and await ping(port, target, auth)  # ...and allowed again
        res = await edit("allow a b")  # junk: refused by the editor, the proxy never hears of it
        assert res.returncode != 0 and "agvisor egress" in res.stderr and await ping(port, target, auth)
        res = await edit("allow other.example.org", "deny other.example.org:25", f"allow {target}")  # several at once
        assert res.returncode == 0 and Path(rules).read_text().split() == ["other.example.org", "!other.example.org:25", target]
        assert await ping(port, target, auth)

        # a bad file (someone wrote to it by hand, then HUP): the old rules stay, the sandbox is neither opened nor shut
        Path(rules).write_text("*\n")  # not an entry
        proc.send_signal(signal.SIGHUP)
        await asyncio.sleep(0.5)
        assert await ping(port, target, auth)

    with tempfile.TemporaryDirectory() as d:
        rules = os.path.join(d, "rules")
        with socket.socket() as s:  # a free port for the proxy
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        async def run():
            origin = await asyncio.start_server(echo, "127.0.0.1", 0)
            op = origin.sockets[0].getsockname()[1]
            env = {**os.environ, "AGVISOR_ALLOW": f"127.0.0.1:{op}", "AGVISOR_TOKEN": "tok", "AGVISOR_RULES": rules, "AGVISOR_PORT": str(port)}
            proc = subprocess.Popen([sys.executable, "-u", p.__file__], env=env, stdout=subprocess.PIPE, text=True)
            try:
                await main(proc, rules, port, op)
            finally:
                proc.terminate()
                out = proc.communicate(timeout=10)[0]
                origin.close()
            return out

        log = asyncio.run(run())
    assert "reload 1 allow 0 deny" in log and "reload 0 allow 1 deny" in log, log  # the reloads, as the host sees them in `docker logs`
    assert "reload failed, keeping the old rules" in log and "deny 127.0.0.1" in log, log  # and the refusal


def test_egress_live_control():
    import signal
    import sys

    # at creation: '!' entries are allowed, but they are not an allowlist by themselves
    Sandbox(egress=["*.x.org", "!admin.x.org", "!10.0.0.0/8:*"])
    for bad in (["!x.org"], ["!", "x.org"], ["x.org", "!!y.org"], ["x.org", "! y.org"]):
        try:
            Sandbox(egress=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted egress={bad}")
    Sandbox(inject={"api.x.com": {"A": "b"}}, egress=["!evil.com"])  # inject's own host is the allow entry
    # the proxy's rules live on a tmpfs it can write (its root is read-only), under its own user and nobody else's
    calls = []
    with mock.patch("agvisor.sandbox.subprocess.run", lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "10.0.0.2\n" if cmd[1] == "inspect" else "", ""))[1]):
        create_sandbox(egress=["pypi.org"]).stop()
    proxy = next(c for c in calls if c[1] == "run" and "python:3.12-slim" in c)
    assert "--tmpfs=/run/agvisor:rw,noexec,nosuid,size=64k,uid=65534,gid=65534,mode=700" in proxy and "--read-only" in proxy

    real_run, state = subprocess.run, {"logs": "", "fail": None}
    with tempfile.TemporaryDirectory() as d:
        rules = Path(d, "rules")
        rules.write_text("pypi.org\n*.pythonhosted.org\n")
        hups, calls = [], []

        def docker(cmd, **kw):
            calls.append((cmd[1:], kw.get("input")))
            if state["fail"] and cmd[1] == "exec":
                return subprocess.CompletedProcess(cmd, 1, "", state["fail"])
            if cmd[1:4] == ["exec", "-i", "agvisor_proxy_x"] and cmd[4:6] == ["python", "-c"]:
                # the very command docker would run in the proxy's container: this file's source, `edit`, the ops on stdin
                r = real_run([sys.executable, "-c", cmd[6], *cmd[7:]], input=kw["input"], text=True, capture_output=True,
                             env={**os.environ, "AGVISOR_RULES": str(rules), "AGVISOR_PID": str(os.getpid())})
                return subprocess.CompletedProcess(cmd, r.returncode, r.stdout, r.stderr)
            if cmd[1:4] == ["exec", "-i", "agvisor_proxy_x"] and cmd[4:] == ["cat", "/run/agvisor/rules"]:
                return subprocess.CompletedProcess(cmd, 0, rules.read_text(), "")
            if cmd[1:3] == ["logs", "agvisor_proxy_x"]:
                return subprocess.CompletedProcess(cmd, 0, state["logs"], "")
            raise AssertionError(f"unexpected docker call {cmd}")

        sb = Sandbox(name="x", egress=["pypi.org", "*.pythonhosted.org"])
        old = signal.signal(signal.SIGHUP, lambda *_: hups.append(1))
        try:
            with mock.patch("agvisor.sandbox.subprocess.run", docker):
                assert sb.egress_rules() == ["pypi.org", "*.pythonhosted.org"]
                sb.allow("Example.COM", "files.example.org:8443")
                assert sb.egress_rules() == ["pypi.org", "*.pythonhosted.org", "example.com", "files.example.org:8443"] and len(hups) == 1
                assert calls[-2][1] == "allow Example.COM\nallow files.example.org:8443\n"  # one exec, one signal, however many entries
                sb.deny("example.com", "*.pythonhosted.org")
                assert sb.egress_rules() == ["pypi.org", "files.example.org:8443", "!example.com", "!*.pythonhosted.org"] and len(hups) == 2
                sb.allow("example.com")  # un-denied
                assert sb.egress_rules()[-1] == "example.com" and "!example.com" not in sb.egress_rules() and len(hups) == 3

                # what was refused, and still is
                state["logs"] = ("allow pypi.org 443\ndeny evil.org 443\ndeny evil.org 443\ndeny example.org 443\ndeny a.pythonhosted.org 443\n"
                                 "fail down.org 443 [Errno 111]\nreload 3 allow 1 deny\ndeny pypi.org 22\ndeny junk\ndeny x.org notaport\n"
                                 "deny example.com 443\ndeny files.example.org 8443\n")
                assert sb.egress_denied() == ["evil.org:443", "example.org:443", "a.pythonhosted.org:443", "pypi.org:22"], sb.egress_denied()
                # example.com and files.example.org were refused once, and are allowed now: not in the list
                sb.allow("evil.org")
                assert sb.egress_denied() == ["example.org:443", "a.pythonhosted.org:443", "pypi.org:22"]
                state["logs"] = ""
                assert sb.egress_denied() == []

                n = len(calls)
                for bad in ((), ("a b",), ("x.org:0",), ("*",), ("ok.org", ""), ("!x.org",)):
                    for fn in (sb.allow, sb.deny):
                        try:
                            fn(*bad)
                        except ValueError:
                            continue
                        raise AssertionError(f"{fn.__name__}{bad} was accepted")
                assert len(calls) == n and len(hups) == 4  # refused here: no docker call, no signal

                state["fail"] = "Error response from daemon: No such container: agvisor_proxy_x"  # the sandbox expired, its proxy is gone
                for fn in (lambda: sb.allow("x.org"), lambda: sb.deny("x.org"), sb.egress_rules, sb.egress_denied):
                    try:
                        fn()
                    except SandboxGone as e:
                        assert "egress proxy" in str(e)
                        continue
                    raise AssertionError("a gone proxy must raise SandboxGone")
                state["fail"] = "OCI runtime exec failed: boom"
                try:
                    sb.allow("x.org")
                except RuntimeError as e:
                    assert "boom" in str(e) and not isinstance(e, SandboxGone)
                else:
                    raise AssertionError("a failing exec must raise")
                state["fail"] = None

                # a bad rules file (the editor refuses it before anything is written): ValueError comes out as a RuntimeError, file intact
                before = rules.read_text()
                rules.write_text("pypi.org\n*\n")  # corrupted behind our back
                try:
                    sb.allow("x.org")
                except RuntimeError as e:
                    assert "agvisor egress" in str(e)
                else:
                    raise AssertionError("an invalid rules file must not be rewritten into a valid-looking one")
                assert rules.read_text() == "pypi.org\n*\n"
                rules.write_text(before)
        finally:
            signal.signal(signal.SIGHUP, old)

    # a sandbox that was made without egress=
    for call in (lambda: Sandbox().allow("x.org"), lambda: Sandbox().deny("x.org"), Sandbox().egress_rules, Sandbox().egress_denied):
        try:
            call()
        except ValueError as e:
            assert "egress" in str(e)
            continue
        raise AssertionError("no allowlist: a ValueError")
    # a handle from list_sandboxes() knows it has a proxy
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    calls2, fake = fake_docker(ps_out=f"agvisor-ab12\trunning\t\t\t{now}\t1\n")
    with mock.patch("agvisor.sandbox.subprocess.run", fake):
        handle = list_sandboxes()[0]
    assert handle._has_egress and handle._proxy == "agvisor_proxy_ab12"


def test_egress_cli_and_mcp():
    import asyncio

    from agvisor import aio, cli, mcp_server as m

    seen = []

    class Fake(Sandbox):
        def allow(self, *entries):
            seen.append(("allow", self.name, entries))
            if entries == ("bad entry",):
                raise ValueError("invalid egress entry 'bad entry'")

        def deny(self, *entries):
            seen.append(("deny", self.name, entries))

        def egress_rules(self):
            return ["pypi.org", "!admin.pypi.org"]

        def egress_denied(self):
            return ["evil.org:443", "x.org:25"]

    def main(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()

    stopped = Fake(name="stopped")
    with mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Fake(name="a")), stopped]):
        assert main("allow", "agvisor-a", "example.com", "*.pythonhosted.org:443") == (0, "done\n")
        assert seen[-1] == ("allow", "agvisor-a", ("example.com", "*.pythonhosted.org:443"))
        assert main("deny", "agvisor-a", "example.com") == (0, "done\n") and seen[-1] == ("deny", "agvisor-a", ("example.com",))
        assert main("allow", "agvisor-a", "bad entry")[0] == 2  # a usage error, not a traceback
        assert main("allow", "agvisor-a")[0] == 2 and main("deny", "agvisor-a")[0] == 2  # an entry is needed
        assert main("allow", "nope", "x.org")[0] == 2 and main("allow", "agvisor-stopped", "x.org")[0] == 2  # no such sandbox / not running
        assert main("egress", "agvisor-a") == (0, "pypi.org\n!admin.pypi.org\n")
        assert main("egress", "agvisor-a", "--denied") == (0, "evil.org:443\nx.org:25\n")
        assert main("egress", "nope")[0] == 2

    # a sandbox without egress=: the library's ValueError is a usage error
    with mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Sandbox(name="plain"))]):
        assert main("allow", "agvisor-plain", "x.org")[0] == 2 and main("egress", "agvisor-plain")[0] == 2

    # aio
    box = Fake(name="b")
    box._has_egress = True
    calls = []
    box.exec = None  # it would be a bug to call it

    async def scenario():
        sb = aio.AsyncSandbox.of(Sandbox(name="b", egress=["pypi.org"]))
        with mock.patch("agvisor.sandbox.subprocess.run", lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "pypi.org\n", ""))[1]):
            assert await sb.egress_rules() == ["pypi.org"]
            assert await sb.egress_denied() == []
    asyncio.run(scenario())
    assert calls[0][1:4] == ["exec", "-i", "agvisor_proxy_b"]

    # MCP: the agent reads what was refused, and has no way to change the rules
    class Box:
        denied = ["evil.org:443", "x.org:25"]
        gone = False

        def egress_denied(self):
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            return self.denied

        def stop(self):
            pass

    box = Box()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.egress_blocked() == "evil.org:443\nx.org:25"
        box.denied = []
        assert m.egress_blocked() == "nothing was refused"
        box.gone = True
        assert m.egress_blocked().startswith("sandbox was reset") and m._sb is None
    m._sb = None
    assert not hasattr(m, "allow") and not hasattr(m, "deny")  # nothing in the server's own namespace that could be registered as a tool


def test_skill_frontmatter_is_strict_yaml():
    # Claude Code and OpenCode forgive a bare `: ` inside a value; VS Code's strict YAML parser rejects the skill
    path = Path(__file__).parent.parent / ".claude/skills/agvisor/SKILL.md"
    if not path.exists():  # the sdist ships tests/ but not .claude/
        return
    front = path.read_text().split("---")[1]
    for line in front.strip().splitlines():
        _, _, val = line.partition(": ")
        if val.startswith(('"', "'")):
            assert val[-1] == val[0] and val[0] not in val[1:-1], f"unclosed or nested quote: {line}"
        else:
            assert ": " not in val and " #" not in val, f"quote this value, a bare ': ' or ' #' breaks YAML: {line}"
    assert "\nname: agvisor\n" in front  # VS Code: `name` must equal the skill's directory name


if __name__ == "__main__":
    test_headless()
    test_gui_start()
    test_gui_setup_failure_cleans_up()
    test_runtime()
    test_create_sandbox_starts_once()
    test_limits_reject_unlimited()
    test_lifetime_and_validation()
    test_named_sandbox_mounts_volume()
    test_resume_and_delete()
    test_snapshot_and_restore()
    test_list_and_delete_snapshot()
    test_failed_resume_keeps_the_container()
    test_idle_ttl()
    test_list_sandboxes()
    test_owner()
    test_workspace_cap()
    test_isolated_network()
    test_exec_raises_when_the_sandbox_is_gone()
    test_exec_streams_output()
    test_put_and_get()
    test_egress_args()
    test_inject_args()
    test_egress_inject()
    test_egress_proxy()
    test_egress_proxy_idle()
    test_jobs()
    test_pool()
    test_put_dir_and_get_dir()
    test_cli()
    test_mcp_server()
    test_mcp_write_file()
    test_mcp_edit_file()
    test_python_session()
    test_mcp_python()
    test_mcp_put_dir()
    test_mcp_jobs()
    test_async()
    test_stats()
    test_mcp_stats()
    test_ports()
    test_mcp_ports()
    test_env()
    test_env_cli_and_mcp()
    test_pause_resume()
    test_idle_watchdog_survives_a_freeze()
    test_renewable_lifetime()
    test_renew_cli()
    test_python_contexts()
    test_mounts()
    test_egress_policy()
    test_egress_live_reload()
    test_egress_live_control()
    test_egress_cli_and_mcp()
    test_skill_frontmatter_is_strict_yaml()
    print("ok")
