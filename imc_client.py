"""
Minimalny klient REST do Aruba/HPE Intelligent Management Center (IMC PLAT eAPI),
dla wersji 7.3+ obsługujących autoryzację tokenem.

WAŻNE — do zweryfikowania w Twoim środowisku:
Dokładne ścieżki endpointów oraz nazwy pól w JSON mogą się nieznacznie różnić między
konkretnymi buildami IMC 7.3.x. Oficjalna dokumentacja REST (Swagger/HTML) jest
zwykle dostępna bezpośrednio w konsoli IMC: System > RESTful API doc (lub katalog
/imcrs/help/ na hoście IMC). Przed użyciem produkcyjnym zweryfikuj poniższe stałe
(ENDPOINT_*) i strukturę payloadu w `add_device` względem tej dokumentacji.

Zaimplementowany przepływ logowania:
    POST {base}/imcrs/plat/access/token   body: {"userName": ..., "password": ...}
    -> odpowiedź zawiera accessToken, który jest dołączany jako nagłówek
       "Access_token" do kolejnych zapytań.
"""

from __future__ import annotations

import json
from typing import Optional

import requests

ENDPOINT_TOKEN = "/imcrs/plat/access/token"
ENDPOINT_ADD_DEVICE = "/imcrs/plat/res/device"
ENDPOINT_LOGOUT = "/imcrs/plat/access/token"  # DELETE na tym samym zasobie


class IMCError(Exception):
    """Błąd komunikacji z IMC lub nieoczekiwana odpowiedź API."""


class IMCClient:
    def __init__(self, host: str, port: str | int, username: str, password: str,
                 verify_ssl: bool = False, use_https: bool = True, timeout: int = 15):
        if not host:
            raise IMCError("Nie podano adresu IMC.")
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.token: Optional[str] = None

        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()  # noqa: type: ignore

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }
        if self.token:
            headers["Access_token"] = self.token
        return headers

    def login(self) -> str:
        url = self.base_url + ENDPOINT_TOKEN
        body = {"userName": self.username, "password": self.password}
        try:
            resp = self.session.post(
                url, data=json.dumps(body), headers=self._headers(),
                verify=self.verify_ssl, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IMCError(f"Nie udało się połączyć z IMC ({url}): {exc}") from exc

        if resp.status_code not in (200, 201):
            raise IMCError(f"Logowanie do IMC nie powiodło się ({resp.status_code}): {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise IMCError(f"Nieoczekiwana odpowiedź logowania IMC (nie-JSON): {resp.text}") from exc

        token = data.get("accessToken") or data.get("token")
        if not token:
            raise IMCError(f"Odpowiedź logowania IMC nie zawiera tokenu: {data}")

        self.token = token
        return token

    def add_device(self, ip: str, label: str = "", contact: str = "", location: str = "",
                    read_community: str = "public", write_community: Optional[str] = None,
                    snmp_version: str = "SNMPv2c") -> dict:
        if not self.token:
            raise IMCError("Brak aktywnego tokenu — wywołaj najpierw login().")

        url = self.base_url + ENDPOINT_ADD_DEVICE
        payload = {
            "ip": ip,
            "label": label,
            "contact": contact,
            "location": location,
            "accessParam": {
                "protocol": snmp_version,
                "readCommunity": read_community,
            },
        }
        if write_community:
            payload["accessParam"]["writeCommunity"] = write_community

        try:
            resp = self.session.post(
                url, data=json.dumps(payload), headers=self._headers(),
                verify=self.verify_ssl, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IMCError(f"Nie udało się połączyć z IMC ({url}): {exc}") from exc

        if resp.status_code not in (200, 201):
            raise IMCError(
                f"Dodanie urządzenia do IMC nie powiodło się ({resp.status_code}): {resp.text}"
            )

        if not resp.text:
            return {"status": "ok", "http_status": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok", "raw_response": resp.text}

    def logout(self) -> None:
        if not self.token:
            return
        url = self.base_url + ENDPOINT_LOGOUT
        try:
            self.session.delete(url, headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        except requests.RequestException:
            pass
        finally:
            self.token = None
