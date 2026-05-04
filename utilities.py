import unicodedata, re, time
import pandas as pd
from asyncio import CancelledError
from pathlib import Path
from datetime import datetime


LOGIN_URL = "https://nexus.t-systems.es/servicedesk/customer/portal/81"
CSV_URL = "https://nexus.t-systems.es/servicedesk/customer/portal/81"
USERNAME = "ramon.cruz@benalmadena.es"
PASSWORD = "Bnmd15.125rc"
AUTH_STATE = Path("auth_state.json")
CURRENT_CODE_FILE = Path("data/current_code.txt")
CSV_PATH = Path("data/initial_photos.csv")
WANTED_COLS = ["Referencia","Resumen","Tipo de solicitud del cliente","Prioridad","Creada"]
DEFAULT_TIMEOUT_MS = 60000
REPORT_URL = "https://nexus.t-systems.es/servicedesk/customer/portal/81"
TEMP_CSV = "data/temp_data.csv"
FINAL_CSV = "data/final_data.csv"
PAUSES_CSV = "data/paradaincidencias.csv"
INITIAL_CSV = "data/initial_photos.csv"
CURRENT_PERIOD_FILE = Path("data/current_period.txt")
DETAIL_URL = "https://nexus.t-systems.es/servicedesk/customer/portal/81/"

# ============= Tokens HTML que identifican cambios de estado =============
CLOSED_TOKENS = {
    "resuelta",
    "cerrada",
    "cancelada",
    "hecho",
    "rechazada",
    "sin liberar",
}
RESOLUTION_TOKENS = {"resuelta"}
RESPONSE_STATUS_TOKENS = {"en curso", "esperando al cliente"}
WAITING_CUSTOMER_TOKENS = {"esperando al cliente"}
PAUSE_STATUS_TOKENS = {"esperando al cliente", "en desarrollo"}

# ============= Tokens de estado de cierre en el CSV =============
CLOSED_STATES_CSV = {"Resolved", "Closed", "Canceled", "Done", "Rejected", "Unreleased"}

# ============= Utilidades para comunicación con el frontend =============
def ensure_not_cancelled(cancel_event) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError()
    
def set_progress(progress_cb, cancel_event, 
                 total_steps, done: int, message: str) -> None:
    ensure_not_cancelled(cancel_event)
    if progress_cb:
        try:
            progress_cb(done, total_steps, message)
        except Exception:
            pass

def sleep_with_cancel(cancel_event, seconds: float) -> None:
    if not cancel_event:
        time.sleep(seconds)
        return
    end_time = time.time() + seconds
    while True:
        remaining = end_time - time.time()
        if remaining <= 0:
            break
        if cancel_event.is_set():
            raise CancelledError()
        time.sleep(min(0.5, remaining))

def report_progress(progress_cb, cancel_event, done: int, total: int, message: str) -> None:
    ensure_not_cancelled(cancel_event)
    if progress_cb:
        try:
            progress_cb(done, total, message)
        except Exception:
            pass

# ============= Utilidades para procesamiento de datos =============
def normalize_label(text: str) -> str:
    """Normaliza etiquetas eliminando tildes, acentos y uso de mayúsculas."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    collapsed = re.sub(r"[^0-9a-zA-Z]+", " ", without_marks)
    return collapsed.strip().lower()

def match_tokens(tokens_1, tokens_2):
    """Comprueba si alguno de los tokens del primer conjunto está dentro del segundo."""
    return bool(tokens_1 & tokens_2)

def parse_time(li_tag):
    '''Función que obtiene la fecha asociada a un li.'''
    time_tag = li_tag.find("time")
    if not time_tag:
        return None

    iso_value = time_tag.get("datetime")
    dt_obj = None
    if iso_value:
        normalized_iso = iso_value.replace("Z", "+00:00")
        tz_match = re.search(r"([+-]\d{2})(:?)(\d{2})$", normalized_iso)
        if tz_match and not tz_match.group(2):
            normalized_iso = (
                normalized_iso[: tz_match.start()]
                + f"{tz_match.group(1)}:{tz_match.group(3)}"
            )
        normalized_iso = re.sub(r"\.\d+(?=[+-]\d{2}:\d{2}$)", "", normalized_iso)
        for candidate in (normalized_iso, iso_value):
            try:
                dt_obj = datetime.fromisoformat(candidate)
                break
            except ValueError:
                try:
                    dt_obj = datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%S%z")
                    break
                except ValueError:
                    dt_obj = None
    if dt_obj is None:
        text_value = time_tag.get_text(strip=True)
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y - %H:%M"):
            try:
                dt_obj = datetime.strptime(text_value, fmt)
                break
            except (TypeError, ValueError):
                continue
    if dt_obj is None:
        return None
    return dt_obj.strftime("%d/%m/%y - %H:%M")

def status_labels(li_tag):
    '''Función que devuelve los cambios de estado 
    asociados a un elemento li.'''
    labels = []
    for strong_tag in li_tag.select("strong"):
        text = strong_tag.get_text(strip=True)
        if text:
            labels.append(normalize_label(text))
    return labels

def has_closed_status(tokens):
    '''Función que, dada la lista de cambios de estado
    de un elemento li, comprueba si hay un estado de cierre.'''
    return match_tokens(tokens, CLOSED_TOKENS)

def has_resolution_status(tokens):
    '''Comprueba si el cambio de estado corresponde a una resolución.'''
    return match_tokens(tokens, RESOLUTION_TOKENS)

def is_pure_close_event(tokens):
    """Detecta si la transición corresponde únicamente a un cierre final."""
    return match_tokens(tokens, {"cerrada"})

def is_development_pause(tokens):
    """Detecta si la transición corresponde únicamente a una pausa de desarrollo."""
    return match_tokens(tokens, {"en desarrollo"})

def duration_to_minutes(value):
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value).strip().lower()
    if not text:
        return 0.0
    if text in {"-", "--"}:
        return 0.0
    if text == "0":
        return 0.0

    normalized_text = (
        text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    ).strip()
    if normalized_text in {"-", "--"}:
        return 0.0

    sign = 1.0
    if normalized_text.startswith("-"):
        sign = -1.0
        normalized_text = normalized_text[1:].strip()
    elif normalized_text.startswith("+"):
        normalized_text = normalized_text[1:].strip()

    if not normalized_text:
        return 0.0

    duration_pattern = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)

    total_minutes = 0.0
    matches = duration_pattern.findall(normalized_text)
    if not matches:
        return 0.0

    for amount_str, unit in matches:
        try:
            amount = float(amount_str)
        except ValueError:
            return 0.0
        unit = unit.lower()
        if unit == "d":
            total_minutes += amount * 24 * 60
        elif unit == "h":
            total_minutes += amount * 60
        elif unit == "m":
            total_minutes += amount
        elif unit == "s":
            total_minutes += amount / 60

    return sign * total_minutes