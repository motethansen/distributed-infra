"""LAN address detection + how it surfaces in /machines."""
import pytest

from shared import netinfo

IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
2: wlp1s0    inet 192.168.0.206/24 brd 192.168.0.255 scope global dynamic wlp1s0\\       valid_lft 7000sec
3: tailscale0    inet 100.112.241.6/32 scope global tailscale0\\       valid_lft forever
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
5: br-99471eacf4b6    inet 172.20.0.1/16 brd 172.20.255.255 scope global br-99471eacf4b6\\       valid_lft forever
6: enp3s0    inet 169.254.7.7/16 scope link enp3s0\\       valid_lft forever
"""

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether f0:18:98:f2:1e:ca
\tinet 192.168.0.21 netmask 0xffffff00 broadcast 192.168.0.255
\tstatus: inactive
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.0.204 netmask 0xffffff00 broadcast 192.168.0.255
\tstatus: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.76.214.54 --> 100.76.214.54 netmask 0xffffffff
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
\tstatus: active
"""


def test_linux_keeps_only_the_real_lan_interface():
    assert netinfo.parse_ip_addr(IP_ADDR) == {"wlp1s0": "192.168.0.206"}


def test_macos_ignores_inactive_ethernet_tailscale_and_bridges():
    # en0 holds a stale manual 192.168.0.21 but is inactive: it must not be reported as reachable
    assert netinfo.parse_ifconfig(IFCONFIG) == {"en1": "192.168.0.204"}


def test_garbage_and_empty_input_do_not_raise():
    assert netinfo.parse_ip_addr("") == {} and netinfo.parse_ifconfig("not ifconfig") == {}


@pytest.fixture
def api(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    import orchestrator.db as dbm
    importlib.reload(dbm)
    import orchestrator.main as m
    importlib.reload(m)
    return m


@pytest.mark.asyncio
async def test_heartbeat_records_lan_ips_and_machines_shows_them(api, monkeypatch, tmp_path):
    cfg = tmp_path / "machines.yaml"
    cfg.write_text("machines:\n  thinkpad-x1:\n    role: worker\n    tailscale_ip: 100.112.241.6\n"
                   "    aliases: [thinkpad]\n    capabilities: [run_script]\n    agents: [claude]\n")
    monkeypatch.setattr(api, "_MACHINES_CONFIG", cfg)
    monkeypatch.setattr(api, "_tailscale_liveness", lambda: {"100.112.241.6": {
        "online": True, "last_seen": None, "path": "direct", "cur_addr": "192.168.0.206:41641"}})
    api._lan_ips.clear()
    await api.worker_heartbeat({"worker_name": "thinkpad", "lan_ips": {"wlp1s0": "192.168.0.206"}}, x_secret_key="k")
    [m] = await api.list_machines(x_secret_key="k")
    assert m["lan_ips"] == {"wlp1s0": "192.168.0.206"}          # found via the alias the worker used
    assert m["tailscale_path"] == "direct" and m["tailscale_addr"] == "192.168.0.206:41641"


@pytest.mark.asyncio
async def test_hostile_lan_ips_payload_is_bounded(api):
    api._lan_ips.clear()
    await api.worker_heartbeat({"worker_name": "w", "lan_ips": {f"i{n}": "x" * 500 for n in range(50)}}, x_secret_key="k")
    assert len(api._lan_ips["w"]) == 8 and all(len(v) <= 45 for v in api._lan_ips["w"].values())
    await api.worker_heartbeat({"worker_name": "w", "lan_ips": "nonsense"}, x_secret_key="k")   # ignored, no crash


def test_tailscale_address_is_excluded_even_on_an_unexpected_interface_name():
    text = "2: wlan0    inet 192.168.0.9/24 scope global wlan0\\\n3: ts9    inet 100.101.102.103/32 scope global ts9\\\n"
    assert netinfo.parse_ip_addr(text) == {"wlan0": "192.168.0.9"}


def test_finds_ifconfig_when_the_service_path_has_no_sbin(monkeypatch):
    """launchd's PATH lacks /sbin, which left mac-mini reporting no addresses at all."""
    monkeypatch.setattr(netinfo.shutil, "which", lambda n: None)
    calls = []

    class R:
        returncode = 0
        stdout = "en1: flags=8863<UP>\n\tinet 192.168.0.204 netmask 0xffffff00\n\tstatus: active\n"

    monkeypatch.setattr(netinfo.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    monkeypatch.setattr(netinfo.os.path, "isfile", lambda p: p == "/sbin/ifconfig")
    monkeypatch.setattr(netinfo.os, "access", lambda p, m: True)
    assert netinfo.lan_ips() == {"en1": "192.168.0.204"}
    assert calls == [["/sbin/ifconfig"]]


def test_own_tailscale_entry_has_no_path(monkeypatch):
    import json, subprocess
    from orchestrator import main as m
    status = {"Self": {"TailscaleIPs": ["100.76.214.54"], "Online": True, "Relay": "sin"},
              "Peer": {"k": {"TailscaleIPs": ["100.112.241.6"], "Online": True, "CurAddr": "192.168.0.206:41641"},
                       "r": {"TailscaleIPs": ["100.97.176.37"], "Online": True, "CurAddr": "", "Relay": "sin"}}}

    class R:
        returncode = 0
        stdout = json.dumps(status)

    monkeypatch.setattr(m, "_tailscale_bin", lambda: "/x/tailscale")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    m._TS_CACHE["ts"] = 0
    out = m._tailscale_liveness()
    assert out["100.76.214.54"]["path"] is None
    assert out["100.112.241.6"]["path"] == "direct"
    assert out["100.97.176.37"]["path"] == "relay:sin"
