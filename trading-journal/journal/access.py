"""Reaching the journal from the phone, without opening it to the world.

Zack captures a bias before the open and reviews trades from an iPhone during
the work day. The machine holding the database is at home. That is a real need
and it deserves a real answer rather than a shrug about loopback.

The answer here has three parts, in order of how much they matter:

  1. **Nothing binds beyond loopback without a token.** The default is
     unchanged: 127.0.0.1, no auth, because there is no remote surface. Asking
     for a wider bind switches on token authentication automatically. There is
     no combination of flags that produces an open unauthenticated service.
  2. **A publicly routable address is refused outright.** Not warned about —
     refused. The journal will bind to loopback, to a Tailscale address, or to
     a private LAN address, and to nothing else.
  3. **The transport is named honestly.** Over Tailscale the traffic is
     encrypted by WireGuard underneath and the token is a second lock. Over
     plain LAN HTTP it is not encrypted, and this module says so out loud
     rather than letting a green padlock be assumed.

Tailscale is the recommended path and this module *detects* it. It does not
install it, does not enable it, and does not touch any network configuration —
it looks at the interface addresses that already exist and reports what it
finds.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from typing import List, Optional

from . import config

TOKEN_FILE = "access.token"
TOKEN_BYTES = 32
COOKIE_NAME = "journal_access"

# Tailscale hands out addresses from the CGNAT range. Seeing one is how we know
# a private mesh already exists without asking the Tailscale daemon anything.
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


class AccessError(RuntimeError):
    pass


# --------------------------------------------------------------------- address
def classify(host: str) -> str:
    """loopback | tailscale | wildcard | private | public | unknown.

    "public" means globally reachable, which is the question that actually
    matters — not merely "outside RFC 1918". `is_global` is the standard
    library's answer to exactly that question, so it is the test used here
    rather than a hand-maintained list of ranges that would drift.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host in ("localhost", "localhost.localdomain"):
            return "loopback"
        return "unknown"
    if ip.is_unspecified:          # 0.0.0.0 / :: — every interface
        return "wildcard"
    if ip.is_loopback:
        return "loopback"
    if ip.version == 4 and ip in TAILSCALE_NET:
        return "tailscale"
    if ip.is_global:
        return "public"
    return "private"


def transport_note(host: str) -> str:
    kind = classify(host)
    if kind == "loopback":
        return "Local only. Nothing leaves the machine."
    if kind == "tailscale":
        return ("Tailscale address. Traffic is encrypted by WireGuard underneath, "
                "and the token is a second lock on top. This is the recommended path.")
    if kind == "private":
        return ("Private network address. The token protects it, but plain HTTP on a "
                "LAN is not encrypted — anyone on the same network can read the "
                "traffic. Prefer Tailscale, which is encrypted end to end.")
    return "Publicly routable. Refused."


def local_addresses() -> List[dict]:
    """Addresses this machine already has, classified. Read-only inspection."""
    found = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except socket.gaierror:
        pass
    # getaddrinfo on macOS often misses the Tailscale interface; a UDP connect
    # to a routable address reveals the outbound source address without sending
    # anything.
    for probe in ("100.100.100.100", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 80))
            found.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    out = []
    for address in sorted(found):
        kind = classify(address.split("%")[0])
        if kind in ("loopback", "tailscale", "private"):
            out.append({"address": address, "kind": kind,
                        "note": transport_note(address.split("%")[0])})
    # Tailscale first, then loopback, then LAN — the order we want to recommend.
    order = {"tailscale": 0, "loopback": 1, "private": 2}
    return sorted(out, key=lambda a: order.get(a["kind"], 9))


def tailscale_available() -> bool:
    return any(a["kind"] == "tailscale" for a in local_addresses())


# --------------------------------------------------------------------- token
def _token_path() -> Path:
    return Path(config.DATA_HOME) / TOKEN_FILE


def get_token(create: bool = False) -> Optional[str]:
    path = _token_path()
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise AccessError(
                f"{path} is readable by others (mode {mode:o}). Run: chmod 600 {path}")
        token = path.read_text().strip()
        if token:
            return token
    if not create:
        return None
    return rotate_token()


def rotate_token() -> str:
    """Mint a new token, invalidating the old one. Written 0600 from the start."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return token


def clear_token() -> bool:
    path = _token_path()
    if path.exists():
        path.unlink()
        return True
    return False


def token_matches(supplied: Optional[str], expected: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


def token_status() -> dict:
    try:
        token = get_token(create=False)
        problem = None
    except AccessError as exc:
        token, problem = None, str(exc)
    return {
        "present": token is not None,
        "problem": problem,
        "location": str(_token_path()),
        "note": "The token is never committed and never written to the request log.",
    }


# --------------------------------------------------------------------- policy
def resolve_bind(host: str, *, token: Optional[str] = None) -> dict:
    """Decide whether this bind is allowed, and what protects it.

    Returns the bind plan. Raises rather than degrading: there is no path
    through this function that yields a non-loopback bind with no token.
    """
    kind = classify(host)

    if kind == "wildcard":
        raise AccessError(
            "refusing to bind every interface. Name the one address the phone "
            "should reach — usually the Tailscale address — so the surface is "
            "the one you intended rather than whatever the network offers.")
    if kind == "public":
        raise AccessError(
            f"refusing to bind {host}: that address is reachable from the public "
            "internet. Use Tailscale (100.x) or a LAN address, or keep loopback.")
    if kind == "unknown":
        raise AccessError(
            f"refusing to bind {host}: not recognisably a loopback, Tailscale or "
            "private address. Give an IP address rather than a name.")

    if kind == "loopback":
        return {"host": host, "kind": kind, "require_token": False, "token": None,
                "transport": transport_note(host)}

    resolved = token or get_token(create=True)
    return {"host": host, "kind": kind, "require_token": True, "token": resolved,
            "transport": transport_note(host)}


def phone_url(host: str, port: int, token: Optional[str]) -> str:
    base = f"http://{host}:{port}/"
    return f"{base}?t={token}" if token else base


def guidance() -> dict:
    """What to do, given what this machine already has. Advice, never action."""
    addresses = local_addresses()
    if tailscale_available():
        recommendation = (
            "Tailscale is already on this machine. Serve on its 100.x address and "
            "open the same address on the phone with Tailscale running. Nothing is "
            "exposed to the internet and the traffic is encrypted.")
    else:
        recommendation = (
            "Tailscale is not set up on this machine, and this project will not "
            "configure it. Installing it is a two-minute job on the Mac and the "
            "iPhone (tailscale.com/download, sign in on both), after which the "
            "phone reaches the journal from anywhere with no ports opened, no "
            "router changes and nothing public. Until then, serving on the LAN "
            "address works at home only, over unencrypted HTTP, with the token "
            "as the sole protection.")
    return {
        "recommendation": recommendation,
        "tailscale_detected": tailscale_available(),
        "addresses": addresses,
        "never": [
            "No port forwarding. No router changes. No tunnel to a public host.",
            "No bind to 0.0.0.0 — the one address is named explicitly.",
            "No unauthenticated bind beyond loopback, under any flag combination.",
        ],
        "token": token_status(),
    }
