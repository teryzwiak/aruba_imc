"""Direct REST client for AOS-CX switches — MAC address verification."""
from __future__ import annotations

import re

import requests

_CANDIDATE_VERSIONS = ["v10.10", "v10.08", "v10.04"]


def normalize_mac(mac: str) -> str:
    """Strip separators and lowercase a MAC address string."""
    return re.sub(r"[^0-9a-fA-F]", "", mac).lower()


def verify_mac(
    ip: str,
    username: str,
    password: str,
    expected_mac: str,
    validate_certs: bool = False,
) -> tuple[bool, str]:
    """
    Connect to AOS-CX REST API at `ip`, retrieve the system MAC and compare
    it with `expected_mac`.

    Returns (matches: bool, actual_mac: str).
    Raises RuntimeError on connectivity/authentication failures.
    """
    if not validate_certs:
        import urllib3
        urllib3.disable_warnings()

    base = f"https://{ip}"
    session = requests.Session()
    chosen_ver: str | None = None
    last_probe: requests.Response | None = None

    for ver in _CANDIDATE_VERSIONS:
        try:
            probe = session.post(
                f"{base}/rest/{ver}/login",
                data={"username": username, "password": password},
                verify=validate_certs,
                timeout=10,
            )
            if probe.status_code == 200:
                chosen_ver = ver
                last_probe = probe
                break
            elif probe.status_code != 404:
                chosen_ver = ver
                last_probe = probe
                break
        except requests.ConnectionError as exc:
            raise RuntimeError(f"Nie można nawiązać połączenia z {base}: {exc}") from exc
        except requests.RequestException:
            continue

    if chosen_ver is None:
        raise RuntimeError("Żadna znana wersja AOS-CX REST API nie odpowiada na tym urządzeniu.")

    if last_probe is None or last_probe.status_code != 200:
        login = session.post(
            f"{base}/rest/{chosen_ver}/login",
            data={"username": username, "password": password},
            verify=validate_certs,
            timeout=10,
        )
        if login.status_code != 200:
            raise RuntimeError(
                f"Logowanie do REST API switcha nie powiodło się "
                f"(HTTP {login.status_code}): {login.text[:200]}"
            )

    try:
        sys_resp = session.get(
            f"{base}/rest/{chosen_ver}/system?attributes=system_mac",
            verify=validate_certs,
            timeout=10,
        )
        if sys_resp.status_code != 200:
            raise RuntimeError(
                f"Nie udało się pobrać system_mac "
                f"(HTTP {sys_resp.status_code}): {sys_resp.text[:200]}"
            )
        data = sys_resp.json()
        actual_mac = data.get("system_mac", "")
        matches = normalize_mac(actual_mac) == normalize_mac(expected_mac)
        return matches, actual_mac
    finally:
        try:
            session.post(
                f"{base}/rest/{chosen_ver}/logout",
                verify=validate_certs,
                timeout=5,
            )
        except Exception:
            pass
