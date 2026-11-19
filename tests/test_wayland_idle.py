"""The Wayland idle client: encoder, decoder and a session against a fake compositor.

The compositor's bytes below were recorded from Hyprland 0.56 (the globals this client
cares about, plus two it must leave alone) and the callback that ends the handshake.
"""
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest

from agent_helpers import AGENT  # noqa: F401  (puts agent/ on sys.path)

import platform_linux as pl

RECORDED = [bytes.fromhex(h) for h in (
    "0200000000001c000100000008000000776c5f736561740009000000",                  # wl_seat 1 v9
    "0200000000002400030000000e000000776c5f636f6d706f7369746f7200000006000000",  # wl_compositor
    "02000000000030000b0000001c0000007a77705f69646c655f696e68696269745f6d616e616765725f7631"
    "0001000000",                                          # zwp_idle_inhibit_manager_v1 11 v1
    "0200000000002c0019000000150000006578745f69646c655f6e6f7469666965725f76310000000002000000",
    "02000000000030001a0000001a000000687970726c616e645f6c6f636b5f6e6f7469666965725f7631000000"
    "01000000",                                             # hyprland_lock_notifier_v1 26 v1
    "0300000000000c00c6780100",                             # wl_callback 3 done
    "0100000001000c0003000000",                             # wl_display.delete_id 3
)]


class CodecTest(unittest.TestCase):
    def test_requests_are_encoded_as_the_protocol_says(self):
        # wl_display.get_registry(new_id 2)
        self.assertEqual(pl.wl_message(1, 1, 2).hex(), "01000000" "01000c00" "02000000")
        # wl_registry.bind(25, "ext_idle_notifier_v1", 1, new_id 5): a 21-byte string, padded
        self.assertEqual(
            pl.wl_message(2, 0, 25, "ext_idle_notifier_v1", 1, 5).hex(),
            "02000000" "00003000" "19000000" "15000000"
            + b"ext_idle_notifier_v1\0".hex() + "000000" "01000000" "05000000")
        # ext_idle_notifier_v1.get_idle_notification(new_id 6, 60000 ms, seat 4)
        self.assertEqual(pl.wl_message(5, 1, 6, 60000, 4).hex(),
                         "05000000" "01001400" "06000000" "60ea0000" "04000000")

    def test_recorded_compositor_bytes_are_decoded_in_any_split(self):
        stream = b"".join(RECORDED)
        for step in (1, 3, 7, 64, len(stream)):
            with self.subTest(step=step):
                msgs, rest = [], b""
                for i in range(0, len(stream), step):
                    got, rest = pl.wl_split(rest + stream[i:i + step])
                    msgs += got
                self.assertEqual(rest, b"")
                self.assertEqual(len(msgs), len(RECORDED))
                globals_ = [pl.wl_args(body, "usu") for obj, op, body in msgs if obj == 2]
                self.assertIn([25, "ext_idle_notifier_v1", 2], globals_)
                self.assertIn([1, "wl_seat", 9], globals_)
                self.assertEqual(msgs[5][:2], (3, 0))

    def test_a_display_error_is_decoded(self):
        body = struct.pack("<II", 6, 3) + struct.pack("<I", 4) + b"bad\0"
        obj, err, msg = pl.wl_args(body, "uus")
        self.assertEqual((obj, err, msg), (6, 3, "bad"))


class FakeCompositor:
    """Answers the handshake with RECORDED (or without the idle global), records every
    request, and can send idled/resumed to the notification the client made."""

    def __init__(self, with_idle=True, with_lock=True):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "wayland-test")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.requests = []
        self.with_idle = with_idle
        self.with_lock = with_lock
        self.subscribed = threading.Event()
        self.conn = None
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        self.conn, _ = self.server.accept()
        buf = b""
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            msgs, buf = pl.wl_split(buf + data)
            for obj, op, body in msgs:
                self.requests.append((obj, op, body))
                if (obj, op) == (1, 0):  # sync: the client has asked for the registry
                    for m in RECORDED:
                        if b"ext_idle_notifier" in m and not self.with_idle:
                            continue
                        if b"hyprland_lock_notifier" in m and not self.with_lock:
                            continue
                        self.conn.sendall(m)
                if op == 1 and obj not in (1, 2) and len(body) == 12:  # get_idle_notification
                    self.notification, self.timeout_ms, self.seat = struct.unpack("<III", body)
                if op == 1 and obj not in (1, 2) and len(body) == 4:  # get_lock_notification
                    self.lock_notification = struct.unpack("<I", body)[0]
                    self.subscribed.set()
                if not self.with_lock and len(body) == 12:
                    self.subscribed.set()

    def send(self, opcode, lock=False):
        obj = self.lock_notification if lock else self.notification
        self.conn.sendall(struct.pack("<II", obj, 8 << 16 | opcode))

    def bound(self):
        return [pl.wl_args(body, "usuu")[1] for obj, op, body in self.requests if obj == 2]

    def close(self):
        if self.conn:
            self.conn.close()
        self.server.close()


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


@unittest.skipUnless(sys.platform.startswith("linux"), "Unix sockets")
class WaylandIdleTest(unittest.TestCase):
    def test_idled_and_resumed_set_the_idle_time(self):
        comp = FakeCompositor(with_lock=False)
        self.addCleanup(comp.close)
        changes = []
        client = pl.WaylandIdle(60, path=comp.path, on_change=lambda: changes.append(1))
        self.assertTrue(client.start())
        self.assertTrue(comp.subscribed.wait(3))
        self.assertEqual(comp.timeout_ms, 60000)
        self.assertEqual(comp.bound(), ["wl_seat", "ext_idle_notifier_v1"])  # no inhibitor
        self.assertEqual(client.idle_seconds(), 0.0)
        comp.send(0)  # idled
        self.assertTrue(wait_for(lambda: client.idle_seconds() >= 60))
        self.assertLess(client.idle_seconds(), 62)
        comp.send(1)  # resumed
        self.assertTrue(wait_for(lambda: client.idle_seconds() == 0.0))
        self.assertEqual(len(changes), 2)

    def test_hyprland_lock_notifications_set_locked(self):
        comp = FakeCompositor()
        self.addCleanup(comp.close)
        client = pl.WaylandIdle(60, path=comp.path)
        self.assertTrue(client.start())
        self.assertTrue(comp.subscribed.wait(3))
        self.assertEqual(comp.bound(), ["wl_seat", "ext_idle_notifier_v1",
                                        "hyprland_lock_notifier_v1"])
        self.assertIs(client.locked, False)
        comp.send(0, lock=True)  # locked
        self.assertTrue(wait_for(lambda: client.locked is True))
        comp.send(1, lock=True)  # unlocked
        self.assertTrue(wait_for(lambda: client.locked is False))

    def test_a_compositor_without_the_protocol_is_reported(self):
        comp = FakeCompositor(with_idle=False)
        self.addCleanup(comp.close)
        client = pl.WaylandIdle(60, path=comp.path)
        self.assertFalse(client.start())
        self.assertIn("ext_idle_notifier_v1", client.error)


@unittest.skipUnless(sys.platform.startswith("linux"), "logind")
class LogindIdleTest(unittest.TestCase):
    """logind's IdleHint, read through busctl (here a stub printing what busctl would)."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        since = int((time.time() - 120) * 1e6)
        script = ("#!/bin/sh\n"
                  'case "$*" in\n'
                  '  *Display*) echo \'{"type":"(so)","data":["2","/org/freedesktop/login1/'
                  'session/_32"]}\' ;;\n'
                  f'  *IdleHint*) echo \'{{"type":"b","data":true}}\'; '
                  f'echo \'{{"type":"t","data":{since}}}\' ;;\n'
                  "  *) exit 1 ;;\nesac\n")
        busctl = os.path.join(self.bin, "busctl")
        with open(busctl, "w") as f:
            f.write(script)
        os.chmod(busctl, stat.S_IRWXU)
        old_path, old_wl = os.environ["PATH"], os.environ.get("WAYLAND_DISPLAY")
        os.environ["PATH"] = self.bin + os.pathsep + old_path
        os.environ.pop("WAYLAND_DISPLAY", None)
        saved = dict(pl._IDLE)

        def restore():
            os.environ["PATH"] = old_path
            if old_wl is not None:
                os.environ["WAYLAND_DISPLAY"] = old_wl
            pl._IDLE.clear()
            pl._IDLE.update(saved)
        self.addCleanup(restore)

    def test_logind_is_the_fallback_without_wayland(self):
        session = pl.logind_session_path()
        self.assertEqual(session, "/org/freedesktop/login1/session/_32")
        self.assertAlmostEqual(pl.logind_idle_seconds(session), 120, delta=2)
        pl._IDLE.clear()
        pl._IDLE["source"] = None
        pl._MODE = "none"
        self.addCleanup(setattr, pl, "_MODE", None)
        self.assertEqual(pl._pick_idle_source(), "logind")
        self.assertAlmostEqual(pl.get_idle_seconds(), 120, delta=2)


if __name__ == "__main__":
    unittest.main()
