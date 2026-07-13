# Aruba AOS-CX Deploy + IMC Registration

Aplikacja Streamlit zastępująca proces ADP (Auto Device Provisioning) w Aruba IMC:
wgrywa konfigurację na switch AOS-CX przez Ansible (REST/httpapi) i rejestruje
urządzenie w Aruba IMC przez REST eAPI (token, IMC 7.3+).

## Instalacja

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
ansible-galaxy collection install arubanetworks.aoscx
```

## Uruchomienie

```bash
streamlit run app.py
```

Aplikacja otworzy się w przeglądarce (domyślnie http://localhost:8501).

## Konfiguracja dostępu

W panelu bocznym podaj:

- **Switch (AOS-CX)** — IP switcha, użytkownika i hasło admina z dostępem do REST API
  (na AOS-CX musi być włączone `https-server rest access-mode read-write`).
- **IMC** — adres, port REST (domyślnie 8080), użytkownika i hasło.

Zamiast wpisywać hasła ręcznie za każdym razem, możesz ustawić zmienne środowiskowe
przed uruchomieniem: `AOSCX_USER`, `AOSCX_PASS`, `IMC_HOST`, `IMC_PORT`, `IMC_USER`, `IMC_PASS`.

## Przepływ pracy

1. Uzupełnij dane urządzenia: IP switcha (aktualne/tymczasowe), nowy hostname,
   VLAN zarządzania + IP/maska, gateway.
2. Dodaj opcjonalne zmienne (klucz/wartość) — trafią do kontekstu renderowania szablonu.
3. Wybierz/edytuj szablon konfiguracji (domyślny w `ansible/templates/aoscx_config.j2`,
   można też wgrać własny plik `.j2`).
4. Sprawdź podgląd wyrenderowanej konfiguracji.
5. Kliknij **Wdróż konfigurację i dodaj do IMC** — aplikacja:
   - uruchamia `ansible-playbook` (plik `ansible/playbook.yml`) targetując podane IP,
   - po sukcesie wywołuje `imc_client.py`, który loguje się do IMC i dodaje urządzenie
     (po nowym IP w VLAN-ie) z podanym community SNMP.

## Ważne — do zweryfikowania przed produkcją

- **IMC REST API**: dokładne ścieżki endpointów i pola JSON w `imc_client.py`
  (`ENDPOINT_TOKEN`, `ENDPOINT_ADD_DEVICE`, struktura `accessParam`) mogą się różnić
  między konkretnymi buildami IMC 7.3.x. Zweryfikuj je względem dokumentacji REST
  dostępnej bezpośrednio w konsoli IMC (System → RESTful API doc) i w razie potrzeby
  popraw stałe na górze pliku `imc_client.py`.
- **AOS-CX REST/httpapi**: switch musi mieć włączony dostęp REST (HTTPS) oraz konto
  z odpowiednimi uprawnieniami. Kolekcja `arubanetworks.aoscx` wymaga zainstalowanej
  biblioteki `pyaoscx` (jest w `requirements.txt`).
- Domyślnie `ansible_httpapi_validate_certs` i weryfikacja SSL dla IMC są wyłączone
  (typowe w środowiskach lab z certyfikatami self-signed) — włącz je w produkcji.
- Szablon `aoscx_config.j2` jest przykładowy — dostosuj go do standardu konfiguracji
  używanego w Twojej organizacji (SNMP, AAA, NTP, porty, itd.).

## Struktura projektu

```
app.py                          # aplikacja Streamlit (frontend + orkiestracja)
imc_client.py                   # klient REST do Aruba IMC (login, add_device)
ansible/
  ansible.cfg
  playbook.yml                  # playbook wgrywający config na AOS-CX
  templates/aoscx_config.j2     # domyślny szablon konfiguracji (Jinja2)
requirements.txt
```
