"""
Aplikacja Streamlit — seryjne wdrożenie wielu switchy Aruba AOS-CX + rejestracja w IMC.

Przepływ dla każdego urządzenia:
1. Weryfikacja MAC adresu przez REST API switcha (musi zgadzać się z podanym).
2. Wyrenderowanie konfiguracji Jinja2 z danymi urządzenia.
3. Wgranie konfiguracji przez Ansible (kolekcja arubanetworks.aoscx, REST/httpapi).
4. Rejestracja urządzenia w Aruba IMC przez REST eAPI (IMC 7.3+, token).

Uruchomienie:
    pip install -r requirements.txt
    ansible-galaxy collection install arubanetworks.aoscx
    streamlit run app.py
"""

import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from jinja2 import Environment, StrictUndefined, meta, nodes

from aoscx_rest import normalize_mac, verify_mac
from imc_client import IMCClient, IMCError

BASE_DIR = Path(__file__).parent
PLAYBOOK = BASE_DIR / "playbook.yml"
TEMPLATES_DIR = BASE_DIR / "templates"

st.set_page_config(page_title="AOS-CX Seryjny Deploy + IMC", layout="wide")
st.title("Seryjne wdrożenie switchy Aruba AOS-CX + rejestracja w IMC")
st.caption(
    "Wgrywa konfigurację na wiele urządzeń po kolei. "
    "Przed wdrożeniem każdego switcha weryfikuje MAC adres przez REST API."
)

# ---------------------------------------------------------------------------
# Sidebar — dane dostępowe (wspólne dla wszystkich urządzeń)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Dostęp do switchy (AOS-CX REST/httpapi)")
    sw_user = st.text_input("Użytkownik admina", value=os.getenv("AOSCX_USER", "admin"))
    sw_pass = st.text_input("Hasło admina", type="password", value=os.getenv("AOSCX_PASS", ""))
    validate_certs = st.checkbox("Waliduj certyfikat SSL switcha", value=False)

    st.divider()
    st.header("Dostęp do Aruba IMC (REST eAPI, token)")
    imc_host = st.text_input("Adres IMC", value=os.getenv("IMC_HOST", ""), placeholder="np. imc.firma.local")
    imc_port = st.text_input("Port IMC REST", value=os.getenv("IMC_PORT", "8080"))
    imc_user = st.text_input("Użytkownik IMC", value=os.getenv("IMC_USER", "admin"))
    imc_pass = st.text_input("Hasło IMC", type="password", value=os.getenv("IMC_PASS", ""))
    imc_verify_ssl = st.checkbox("Waliduj certyfikat SSL IMC", value=False)

st.divider()

# ---------------------------------------------------------------------------
# Krok 1: lista urządzeń
# ---------------------------------------------------------------------------
st.subheader("1. Urządzenia do wdrożenia")
st.caption(
    "Dodaj wiersze dla każdego switcha. MAC adres jest weryfikowany na urządzeniu przed wdrożeniem. "
    "Jeśli Gateway jest pusty — użyty zostanie Wspólny gateway z sekcji poniżej."
)

_DEVICE_COLUMNS = [
    "IP switcha",
    "MAC adres",
    "Nowy hostname",
    "IP w VLAN",
    "Gateway (opcjonalnie)",
]
_DEVICE_COLUMN_ALIASES = {
    "IP switcha": ["ip switcha", "ip switch", "ip", "adres ip"],
    "MAC adres": ["mac adres", "mac", "mac address", "adres mac"],
    "Nowy hostname": ["nowy hostname", "hostname", "nazwa"],
    "IP w VLAN": ["ip w vlan", "vlan ip", "ip vlan"],
    "Gateway (opcjonalnie)": ["gateway (opcjonalnie)", "gateway", "brama"],
}
_EMPTY_DEVICES = pd.DataFrame({col: [""] for col in _DEVICE_COLUMNS})


def _devices_excel_template() -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(columns=_DEVICE_COLUMNS).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _parse_devices_excel(file) -> pd.DataFrame:
    excel_df = pd.read_excel(file, engine="openpyxl")
    excel_df.columns = [str(c).strip() for c in excel_df.columns]
    lower_cols = {c.lower(): c for c in excel_df.columns}

    rename_map = {}
    for target, aliases in _DEVICE_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower_cols:
                rename_map[lower_cols[alias]] = target
                break
    excel_df = excel_df.rename(columns=rename_map)

    for col in _DEVICE_COLUMNS:
        if col not in excel_df.columns:
            excel_df[col] = ""
    excel_df = excel_df[_DEVICE_COLUMNS].fillna("")
    return excel_df.astype(str).replace("nan", "")


if "devices_df_base" not in st.session_state:
    st.session_state["devices_df_base"] = _EMPTY_DEVICES
if "devices_table_version" not in st.session_state:
    st.session_state["devices_table_version"] = 0

upload_col, template_col = st.columns([3, 1])
with upload_col:
    uploaded_devices_file = st.file_uploader(
        "Wgraj plik Excel z urządzeniami (kolumny: IP switcha, MAC adres, Nowy hostname, "
        "IP w VLAN, Gateway (opcjonalnie))",
        type=["xlsx", "xls"],
        key="devices_excel_uploader",
    )
with template_col:
    st.write("")
    st.download_button(
        "Pobierz szablon Excel",
        data=_devices_excel_template(),
        file_name="szablon_urzadzenia.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

if (
    uploaded_devices_file is not None
    and st.session_state.get("_devices_excel_name") != uploaded_devices_file.name
):
    try:
        st.session_state["devices_df_base"] = _parse_devices_excel(uploaded_devices_file)
        st.session_state["devices_table_version"] += 1
        st.session_state["_devices_excel_name"] = uploaded_devices_file.name
        st.success(f"Wczytano {len(st.session_state['devices_df_base'])} urządzeń z pliku.")
    except Exception as exc:
        st.error(f"Błąd wczytywania pliku Excel: {exc}")

devices_df: pd.DataFrame = st.data_editor(
    st.session_state["devices_df_base"],
    num_rows="dynamic",
    use_container_width=True,
    key=f"devices_table_{st.session_state['devices_table_version']}",
)

# ---------------------------------------------------------------------------
# Krok 2: wspólna konfiguracja
# ---------------------------------------------------------------------------
st.subheader("2. Wspólna konfiguracja sieci")
col1, col2 = st.columns(2)
with col1:
    vlan_id = st.number_input("VLAN ID (zarządzania)", min_value=1, max_value=4094, value=10)
    vlan_prefix = st.number_input("Maska (prefix, CIDR)", min_value=1, max_value=32, value=24)
    gateway_ip = st.text_input("Wspólny gateway", placeholder="np. 10.10.10.1")
with col2:
    snmp_ro = st.text_input("SNMP community RO (do dodania w IMC)", value="public")
    snmp_rw = st.text_input("SNMP community RW (opcjonalnie)", value="")
    ntp_server = st.text_input("Serwer NTP (opcjonalnie)", value="")

st.subheader("3. Dodatkowe zmienne (opcjonalnie)")
st.caption("Dowolne pary klucz/wartość używane w szablonie configu, wspólne dla wszystkich urządzeń.")
extra_vars_editor = st.data_editor(
    {"klucz": [""], "wartość": [""]},
    num_rows="dynamic",
    use_container_width=True,
    key="extra_vars_editor",
)

# ---------------------------------------------------------------------------
# Krok 3: szablon
# ---------------------------------------------------------------------------
st.subheader("4. Szablon konfiguracji (Jinja2, składnia CLI AOS-CX)")

_TEMPLATE_LABELS = {
    "standardowy": "Standardowy (VLAN zarządzania + gateway)",
    "switch_dostepowy": "Switch dostępowy (porty access)",
    "uplink_trunk": "Uplink / trunk (LAG do rdzenia)",
}


def _list_templates() -> dict[str, Path]:
    if not TEMPLATES_DIR.is_dir():
        return {}
    files = sorted(TEMPLATES_DIR.glob("*.j2"))
    return {_TEMPLATE_LABELS.get(p.stem, p.stem.replace("_", " ").capitalize()): p for p in files}


available_templates = _list_templates()

if "template_source_key" not in st.session_state:
    st.session_state["template_source_key"] = None
if "template_text_version" not in st.session_state:
    st.session_state["template_text_version"] = 0
if "template_base_text" not in st.session_state:
    if available_templates:
        st.session_state["template_base_text"] = next(iter(available_templates.values())).read_text(
            encoding="utf-8"
        )
    else:
        st.session_state["template_base_text"] = ""

col_select, col_upload = st.columns(2)
with col_select:
    selected_template_label = st.selectbox(
        "Predefiniowany szablon (z folderu templates/)",
        options=list(available_templates.keys()) or ["(brak szablonów w folderze templates/)"],
        disabled=not available_templates,
        help="Szablony wczytywane z plików .j2 w folderze templates/ w katalogu projektu.",
    )
with col_upload:
    uploaded_template = st.file_uploader("...lub wgraj własny szablon .j2", type=["j2", "txt", "cfg"])

if uploaded_template is not None:
    source_key = f"upload:{uploaded_template.name}:{uploaded_template.size}"
    if st.session_state["template_source_key"] != source_key:
        st.session_state["template_base_text"] = uploaded_template.read().decode("utf-8")
        st.session_state["template_source_key"] = source_key
        st.session_state["template_text_version"] += 1
elif available_templates:
    source_key = f"preset:{selected_template_label}"
    if st.session_state["template_source_key"] != source_key:
        st.session_state["template_base_text"] = available_templates[selected_template_label].read_text(
            encoding="utf-8"
        )
        st.session_state["template_source_key"] = source_key
        st.session_state["template_text_version"] += 1

template_text = st.text_area(
    "Treść szablonu (edytowalna)",
    value=st.session_state["template_base_text"],
    height=320,
    key=f"template_text_area_{st.session_state['template_text_version']}",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extra_vars() -> dict:
    keys = extra_vars_editor.get("klucz", [])
    vals = extra_vars_editor.get("wartość", [])
    return {k: v for k, v in zip(keys, vals) if k}


def build_context(hostname: str, vlan_ip: str, effective_gateway: str) -> dict:
    ctx = {
        "hostname": hostname,
        "vlan_id": int(vlan_id),
        "vlan_ip": vlan_ip,
        "vlan_prefix": int(vlan_prefix),
        "gateway_ip": effective_gateway,
        "ntp_server": ntp_server,
    }
    ctx.update(_extra_vars())
    return ctx


def _optional_variable_names(ast: nodes.Template) -> set[str]:
    """Zmienne używane wyłącznie w testach `is defined` / `is not defined`
    nie są traktowane jako wymagane — szablon sam obsługuje ich brak."""
    return {
        node.node.name
        for node in ast.find_all(nodes.Test)
        if node.name in ("defined", "undefined") and isinstance(node.node, nodes.Name)
    }


def render_config(template_str: str, context: dict) -> str:
    env = Environment(undefined=StrictUndefined)
    ast = env.parse(template_str)
    optional = _optional_variable_names(ast)
    missing = [v for v in meta.find_undeclared_variables(ast) if v not in context and v not in optional]
    if missing:
        raise ValueError(f"Szablon wymaga zmiennych, których nie podano: {', '.join(missing)}")
    return env.from_string(template_str).render(**context)


def run_ansible(
    config_lines: list[str],
    target_ip: str,
    username: str,
    password: str,
    validate_ssl: bool,
    new_hostname: str,
) -> subprocess.CompletedProcess:
    inventory_content = (
        "[target_switch]\n"
        f"switch ansible_host={target_ip}\n\n"
        "[target_switch:vars]\n"
        "ansible_connection=httpapi\n"
        "ansible_network_os=arubanetworks.aoscx.aoscx\n"
        f"ansible_user={username}\n"
        f"ansible_password={password}\n"
        "ansible_httpapi_use_ssl=yes\n"
        f"ansible_httpapi_validate_certs={'yes' if validate_ssl else 'no'}\n"
        "ansible_httpapi_port=443\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.ini"
        inv_path.write_text(inventory_content, encoding="utf-8")

        ev_path = Path(tmpdir) / "extra_vars.json"
        ev_path.write_text(
            json.dumps({"config_lines": config_lines, "new_hostname": new_hostname}),
            encoding="utf-8",
        )

        return subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(PLAYBOOK), "-e", f"@{ev_path}"],
            capture_output=True,
            text=True,
        )


def _valid_devices(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, row in df.iterrows():
        ip = str(row.get("IP switcha", "")).strip()
        mac = str(row.get("MAC adres", "")).strip()
        hostname = str(row.get("Nowy hostname", "")).strip()
        vlan_ip = str(row.get("IP w VLAN", "")).strip()
        gw = str(row.get("Gateway (opcjonalnie)", "")).strip() or gateway_ip
        if ip and mac and hostname and vlan_ip:
            rows.append({"ip": ip, "mac": mac, "hostname": hostname, "vlan_ip": vlan_ip, "gateway": gw})
    return rows


# ---------------------------------------------------------------------------
# Krok 4: podgląd (pierwsze urządzenie z tabeli)
# ---------------------------------------------------------------------------
st.subheader("5. Podgląd konfiguracji (pierwsze urządzenie z listy)")
valid_for_preview = _valid_devices(devices_df)
preview_error: str = ""
rendered_preview: str = ""

if valid_for_preview:
    first = valid_for_preview[0]
    try:
        ctx = build_context(first["hostname"], first["vlan_ip"], first["gateway"])
        rendered_preview = render_config(template_text, ctx)
        st.caption(f"Podgląd dla: **{first['hostname']}** ({first['ip']})")
        st.code(rendered_preview, language="text")
    except Exception as exc:
        preview_error = str(exc)
        st.error(f"Błąd renderowania szablonu: {preview_error}")
else:
    st.info("Dodaj urządzenia w tabeli powyżej, aby zobaczyć podgląd konfiguracji.")

# ---------------------------------------------------------------------------
# Krok 5: seryjne wdrożenie
# ---------------------------------------------------------------------------
st.divider()
st.subheader("6. Seryjne wdrożenie")

devices_to_deploy = _valid_devices(devices_df)
deploy_disabled = (
    not devices_to_deploy
    or not sw_user
    or bool(preview_error)
)

if deploy_disabled:
    if not devices_to_deploy:
        st.info("Dodaj co najmniej jedno urządzenie (IP, MAC, hostname, IP w VLAN) do tabeli.")
    elif not sw_user:
        st.warning("Podaj dane logowania do switchy w panelu bocznym.")
    elif preview_error:
        st.warning("Popraw błędy szablonu przed wdrożeniem.")

if st.button(
    f"Wdróż konfigurację na {len(devices_to_deploy)} urządzeń i dodaj do IMC",
    disabled=deploy_disabled,
    type="primary",
):
    progress = st.progress(0, text="Rozpoczynanie...")
    ok_count = 0
    fail_count = 0

    for idx, device in enumerate(devices_to_deploy):
        progress.progress(
            idx / len(devices_to_deploy),
            text=f"Przetwarzanie {idx + 1}/{len(devices_to_deploy)}: {device['hostname']} ({device['ip']})",
        )

        st.markdown(f"---\n#### Urządzenie {idx + 1}: `{device['hostname']}` — {device['ip']}")
        device_ok = True

        # --- weryfikacja MAC ---
        with st.status("Weryfikacja MAC adresu...", expanded=True) as mac_status:
            try:
                matches, actual_mac = verify_mac(
                    ip=device["ip"],
                    username=sw_user,
                    password=sw_pass,
                    expected_mac=device["mac"],
                    validate_certs=validate_certs,
                )
                if matches:
                    mac_status.update(
                        label=f"MAC zweryfikowany: {actual_mac}",
                        state="complete",
                    )
                else:
                    mac_status.update(
                        label=(
                            f"MAC nie pasuje — oczekiwano: {device['mac']}, "
                            f"urządzenie zwróciło: {actual_mac}"
                        ),
                        state="error",
                    )
                    device_ok = False
            except RuntimeError as exc:
                mac_status.update(label=f"Błąd weryfikacji MAC: {exc}", state="error")
                device_ok = False

        if not device_ok:
            st.error(f"Pomijam {device['hostname']} — weryfikacja MAC nie powiodła się.")
            fail_count += 1
            continue

        # --- renderowanie konfiguracji ---
        try:
            ctx = build_context(device["hostname"], device["vlan_ip"], device["gateway"])
            rendered = render_config(template_text, ctx)
        except Exception as exc:
            st.error(f"Błąd renderowania konfiguracji dla {device['hostname']}: {exc}")
            fail_count += 1
            continue

        config_lines = [line for line in rendered.splitlines() if line.strip()]

        # --- Ansible ---
        with st.status("Wgrywanie konfiguracji przez Ansible...", expanded=True) as ans_status:
            result = run_ansible(
                config_lines,
                device["ip"],
                sw_user,
                sw_pass,
                validate_certs,
                device["hostname"],
            )
            st.text("STDOUT:")
            st.code(result.stdout or "(brak)")
            if result.stderr:
                st.text("STDERR:")
                st.code(result.stderr)

            if result.returncode != 0:
                ans_status.update(label="Błąd podczas wdrażania konfiguracji przez Ansible", state="error")
                fail_count += 1
                device_ok = False
            else:
                ans_status.update(label="Konfiguracja wgrana pomyślnie", state="complete")

        if not device_ok:
            continue

        # --- IMC ---
        with st.status("Rejestracja urządzenia w Aruba IMC...", expanded=True) as imc_status:
            try:
                client = IMCClient(
                    host=imc_host,
                    port=imc_port,
                    username=imc_user,
                    password=imc_pass,
                    verify_ssl=imc_verify_ssl,
                )
                client.login()
                resp = client.add_device(
                    ip=device["vlan_ip"],
                    label=device["hostname"],
                    read_community=snmp_ro,
                    write_community=snmp_rw or None,
                )
                st.json(resp)
                client.logout()
                imc_status.update(label="Urządzenie dodane do IMC", state="complete")
                ok_count += 1
            except IMCError as exc:
                imc_status.update(label=f"Błąd podczas dodawania do IMC: {exc}", state="error")
                st.warning(
                    "Sprawdź dokładne ścieżki/pola REST API względem dokumentacji Twojej instancji IMC "
                    "(System → RESTful API doc w konsoli IMC)."
                )
                fail_count += 1

    progress.progress(1.0, text="Zakończono")
    st.divider()
    if fail_count == 0:
        st.success(f"Wszystkie {ok_count} urządzeń wdrożone pomyślnie.")
    else:
        st.warning(f"Zakończono: {ok_count} OK, {fail_count} błędów/pominiętych.")
