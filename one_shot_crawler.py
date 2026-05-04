import re
import pandas as pd
from asyncio import CancelledError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright.sync_api import expect, Error
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, date
from tqdm import tqdm
from typing import List, Callable, Optional
from threading import Event
from utilities import TEMP_CSV, FINAL_CSV, PAUSES_CSV, DETAIL_URL, DEFAULT_TIMEOUT_MS
from utilities import normalize_label, ensure_not_cancelled, set_progress
from utilities import sleep_with_cancel, duration_to_minutes, report_progress
from utilities import match_tokens, parse_time, status_labels
from utilities import PAUSE_STATUS_TOKENS, RESPONSE_STATUS_TOKENS, WAITING_CUSTOMER_TOKENS
from utilities import has_closed_status, has_resolution_status, is_pure_close_event
from utilities import CLOSED_STATES_CSV, is_development_pause
from crawler import Crawler


class OneShotCrawler(Crawler):
    def __init__(self):
        super().__init__()

    def scrape_csv(
        self,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[Event] = None,
    ):
        total_steps = 5
        completed_steps = 0

        def mark_step(message: str) -> None:
            nonlocal completed_steps
            completed_steps = min(completed_steps + 1, total_steps)
            set_progress(progress_cb, cancel_event, total_steps, completed_steps, message)

        ensure_not_cancelled(cancel_event)
        set_progress(progress_cb, cancel_event, total_steps, 0, "Iniciando descarga del CSV…")

        with sync_playwright() as p:
            ensure_not_cancelled(cancel_event)
            browser = p.chromium.launch(headless=True)
            context = (browser.new_context())
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page = context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
            current_step = "init"

            def debug(step: str) -> None:
                try:
                    print(f"[DEBUG] {step} | url={page.url}")
                except Exception:
                    print(f"[DEBUG] {step} | url=<unavailable>")

            print("Scrapeando CSV...")

            try:
                # 1) Login
                current_step = "authenticate"
                debug("Antes de authenticate")
                ensure_not_cancelled(cancel_event)
                self.authenticate(page)
                debug("Después de authenticate")
                mark_step("Autenticación completada...")

                # 2) Acceder a la vista de visualización de incidencias
                current_step = "access_adv_report"
                debug("Antes de access_adv_report")
                ensure_not_cancelled(cancel_event)
                self.access_adv_report(page)
                debug("Después de access_adv_report")
                mark_step("Vista de incidencias cargada...")

                # 3) Abrir dropdown de exportación
                current_step = "open_export_dropdown"
                debug("Abriendo dropdown de exportación")
                ensure_not_cancelled(cancel_event)
                export_button = page.locator('button[data-testid="apr-export-btn"]')
                export_button.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
                export_button.click()

                # Esperar a que el panel del dropdown sea visible
                try:
                    panel = page.locator('div[id^="ds--dropdown--"]')
                    expect(panel).to_be_visible(timeout=DEFAULT_TIMEOUT_MS)  # assertion espera visibilidad
                except Exception:
                    # fallback: espera genérica por algún elemento del menú
                    page.locator("role=menu,div[role='menu']").first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
                mark_step("Menú de exportación disponible...")

                # 4) Pulsar el botón de descarga dentro del dropdown
                current_step = "click_export_csv"
                debug("Buscando opción CSV")
                ensure_not_cancelled(cancel_event)
                export_csv = page.get_by_role("menuitem", name=re.compile(r"csv", re.I))
                export_csv.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
                with page.expect_download(timeout=DEFAULT_TIMEOUT_MS) as dl_info:
                    export_csv.click()
                download = dl_info.value
                mark_step("Descarga iniciada...")

                # Guardar de forma persistente ANTES de cerrar el contexto/navegador
                # download.save_as(Path(f"{TEMP_CSV}_{datetime.today.strftime("%Y-%m-%d_%H:%M:%S")}.csv"))
                download.save_as(Path(TEMP_CSV))
                mark_step("CSV guardado correctamente...")

            except PWTimeout as e:
                print(f"[Timeout] en paso '{current_step}': {e}")
                try:
                    print(f"[Timeout] URL actual: {page.url}")
                except Exception:
                    print("[Timeout] URL actual no disponible")
                try:
                    print(f"[Timeout] Título actual: {page.title()}")
                except Exception:
                    pass
                set_progress(progress_cb, cancel_event, total_steps, completed_steps, f"Timeout en el paso {current_step}.")
            finally:
                context.close()
                browser.close()

    def load_df(self, csv_path: str) -> pd.DataFrame:
        if (not Path(csv_path).exists()):
            raise CancelledError(
                ("Tarea cancelada. El CSV no ha podido ser descargado correctamente, "
                 "seguramente debido a un timeout. Pruebe a lanzar el scraping "
                 "de nuevo.")
            )

        print("Cargando y limpiando el dataframe de incidencias totales...")

        df = pd.read_csv(csv_path)
        df = df.loc[:, ["Reference","Resumen","Tipo de solicitud del cliente",
            "Prioridad","Creada","Informador","Estado",
            "Tiempo hasta primera respuesta: Remaining Time",
            "Tiempo hasta resolución: Remaining Time",
            "Tiempo hasta primera respuesta: Goal",
            "Tiempo hasta resolución: Goal",
        ]].copy()

        # Arreglamos el tipo de dato de la fecha de creación del df
        df["Creada"] = pd.to_datetime(
            df["Creada"],
            format="%d/%m/%y - %H:%M",
            errors="coerce"       
        )

        # Columnas temporales del dataframe, para su posterior conversión
        duration_columns = [
            "Tiempo hasta primera respuesta: Remaining Time",
            "Tiempo hasta resolución: Remaining Time",
            "Tiempo hasta primera respuesta: Goal",
            "Tiempo hasta resolución: Goal"
        ]

        # Arreglamos el formato de las columnas de tiempo a columnas numéricas 
        # con los minutos equivalentes
        for column in duration_columns:
            df[column] = pd.to_numeric(
                df[column].apply(duration_to_minutes),
                errors="coerce"
            )

        # Pasamos ahora al cálculo de los tiempos objetivo de acuerdo con los
        # términos del contrato
        priority_series = df["Prioridad"].fillna("").astype(str).str.strip()
        response_goal_map = {
            "Blocker": 120,
            "Critical": 120,
            "Major": 240,
            "Medium": 240,
            "Minor": 240,
            "Trivial": 240
        }
        resolution_goal_map = {
            "Blocker": 1440,
            "Critical": 1440,
            "Major": 3600,
            "Medium": 7200,
            "Minor": 7200,
            "Trivial": 7200
        }
        df["Tiempo hasta primera respuesta: Goal (Contrato)"] = (
            priority_series.map(response_goal_map).fillna(240).astype(int)
        )
        df["Tiempo hasta resolución: Goal (Contrato)"] = (
            priority_series.map(resolution_goal_map).fillna(7200).astype(int)
        )

        # Convertimos el tiempo restante a tiempo consumido siguiendo t_new = goal - t_old
        remaining_goal_pairs = [
            (
                "Tiempo hasta primera respuesta: Remaining Time",
                "Tiempo hasta primera respuesta: Goal",
            ),
            (
                "Tiempo hasta resolución: Remaining Time",
                "Tiempo hasta resolución: Goal",
            ),
        ]

        # Se dejan los valores 0.0 sin modificar
        for remaining_col, goal_col in remaining_goal_pairs:
            remaining_float = df[remaining_col].astype(float)
            goal_float = df[goal_col].astype(float)
            consumed_time = goal_float - remaining_float
            df[remaining_col] = consumed_time.where(remaining_float != 0.0, remaining_float)

        # df.to_csv("data/intermediate_result.csv", index=False)
        return df
    
    def extract_dates(self, html: str, current_date: str, is_closed: bool):
        '''Devuelve las fechas de primera respuesta y resolución, así como las pausas
        correspondientes para el tiempo de resolución de una incidencia dada.'''
        soup = BeautifulSoup(html, "lxml")
        result_dates = {"response_date": current_date, "resolved_date": current_date}
        resolution_pauses = {"stop_date": [], "restart_date": [], 
                             "is_development": []}

        # Obtiene todo el histórico de actividad de la incidencia
        activity_list = soup.select_one("ul.vp-activity-list")
        if not activity_list:
            result_dates["resolution_pauses"] = resolution_pauses
            return result_dates

        # Obtiene todos los elementos <li> de primer nivel para evitar los
        # listados anidados presentes en comentarios.
        items = list(activity_list.find_all("li", recursive=False))
        if not items:
            result_dates["resolution_pauses"] = resolution_pauses
            return result_dates

        # Flag que comprueba si se ha encontrado un cambio
        # de estado de tiempo de primera respuesta.
        response_found = False
        closure_chain = []

        # Iteramos el histórico de actividad en orden inverso, elemento
        # li a elemento li. 
        reversed_items = list(reversed(items))
        for idx, li_tag in enumerate(reversed_items):
            # Obtenemos la fecha asociada al elemento li.
            time_str = parse_time(li_tag)
            if not time_str:
                continue
            
            # Obtenemos las clases de los elementos div para discriminar
            # entre cambios de status o comentarios de trabajador/cliente,
            # así como los propios cambios de status.
            activity_div = li_tag.find("div", class_="activity-item")
            classes = set(activity_div.get("class", [])) if activity_div else set()
            labels = status_labels(li_tag)
            labels_normalized = {normalize_label(label) for label in labels}

            # Realizamos diferentes comprobaciones sobre el li actual
            # para lógica posterior.
            is_closed_status = has_closed_status(labels_normalized)
            is_resolution_status = has_resolution_status(labels_normalized)
            is_worker_comment = "worker-comment" in classes
            is_customer_comment = "requester-comment" in classes
            is_comment = is_worker_comment or is_customer_comment
            is_status_change_event = bool(labels_normalized) and not is_comment

            # Comenzamos comprobando si es cierre. De serlo, lo añadimos
            # a la lista de cláusulas de cierre; en cuanto nos encontramos
            # con un <li> posterior, limpiamos la estructura ya que no
            # será un cierre final.
            if is_status_change_event:
                if is_closed_status:
                    closure_chain.append(
                        {
                            "time": time_str,
                            "tokens": labels_normalized,
                            "is_resolution": is_resolution_status,
                        }
                    )
                else:
                    closure_chain.clear()

            # Comprobamos si el li actual es un comentario de trabajador,
            # tiene cláusula de cierre que se ha producido al final o si 
            # tiene una cláusula de primera respuesta. Puesto que solo
            # podemos tener una primera respuesta, cerramos la flag
            # en cuanto encontramos una.
            response_update = (
                is_closed_status
                or is_worker_comment
                or match_tokens(labels_normalized, RESPONSE_STATUS_TOKENS)
            )

            if not response_found and response_update:
                result_dates["response_date"] = time_str
                response_found = True

            # Exploramos ahora las posibles pausas en el tiempo
            # de resolución.
            pause_update = (
                is_closed_status
                or match_tokens(labels_normalized, PAUSE_STATUS_TOKENS)
            )

            if pause_update:
                next_time: Optional[str] = None
                is_waiting_customer = (
                    match_tokens(labels_normalized, WAITING_CUSTOMER_TOKENS)
                )

                # Comprobamos si es pausa de desarrollo para la condición
                # de que el desarrollo no puede ser mayor de 6 meses.
                is_development = is_development_pause(labels_normalized)

                for next_li in reversed_items[idx + 1:]:
                    # Comprobamos que no sea un comentario de trabajador
                    # para el caso de pausa tipo esperando a cliente
                    activity_div = next_li.find("div", class_="activity-item")
                    classes = set(activity_div.get("class", [])) if activity_div else set()
                    next_is_worker_comment = "worker-comment" in classes
                    next_is_customer_comment = "requester-comment" in classes

                    # Únicamente paramos si hay tiempo siguiente y si no es
                    # un comentario de trabajador en el caso de que sea
                    # una pausa de tipo esperando a cliente
                    if is_waiting_customer and not next_is_worker_comment:
                        # Obtenemos el tiempo del siguiente li
                        candidate_time = parse_time(next_li)
                        if candidate_time:
                            next_time = candidate_time
                            break
                        continue
                    # Comprobamos ahora el caso de que no sea una pausa de tipo
                    # esperando a cliente. Para cualquier casuística de este tipo
                    # no tenemos en cuenta ni comentarios de trabajador ni de cliente
                    if not is_waiting_customer and (not (next_is_worker_comment or 
                                                    next_is_customer_comment)):
                        candidate_time = parse_time(next_li)
                        if candidate_time:
                            next_time = candidate_time
                            break
                        continue

                if next_time is None:
                    if is_closed_status:
                        # Un cierre sin un estado posterior indica cierre definitivo,
                        # por lo que no debemos registrar una pausa.
                        continue
                    next_time = current_date

                resolution_pauses["stop_date"].append(time_str)
                resolution_pauses["restart_date"].append(next_time)
                resolution_pauses["is_development"].append(is_development)

        # Si la incidencia está cerrada, intentamos encontrar el tiempo
        # de resolución. La prioridad es la siguiente: las resoluciones
        # puras siempre tienen la prioridad frente al resto de cierres;
        # por eso se comprueba primero si hay cierre definitivo y luego
        # si hay resolución, la cual sobreescribe el resultado anterior.
        if is_closed:
            resolved_time: Optional[str] = None
            if closure_chain:
                for event in reversed(closure_chain):
                    if (not is_pure_close_event(event["tokens"]) and 
                                not event["is_resolution"]):
                        resolved_time = event["time"]
                        break
                for event in reversed(closure_chain):
                    if event["is_resolution"]:
                        resolved_time = event["time"]
                        break
                if resolved_time is None:
                    resolved_time = closure_chain[-1]["time"]
            if resolved_time:
                result_dates["resolved_date"] = resolved_time

        result_dates["resolution_pauses"] = resolution_pauses
        return result_dates
    
    def get_closed(
            self, cancel_event, progress_cb,
            last_date: pd.Timestamp,
            current_date: pd.Timestamp,
            global_closed: List[str]
        ) -> List[str]:
        """Devuelve las incidencias cerradas dentro del período actual."""
        ensure_not_cancelled(cancel_event)
        results: List[str] = []

        with sync_playwright() as p:
            ensure_not_cancelled(cancel_event)
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            html = ""
            try:
                ensure_not_cancelled(cancel_event)
                self.authenticate(page)

                total_refs = len(global_closed)
                progress_total = max(total_refs, 1)

                with tqdm(total=len(global_closed), desc="Incidencias cerradas comprobadas") as pbar:
                    processed = 0
                    for ref in global_closed:
                        ensure_not_cancelled(cancel_event)

                        detail_url = f"{DETAIL_URL}{ref}"
                        try:
                            page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
                            page.wait_for_selector("ul.vp-activity-list li", timeout=30_000)
                        except PWTimeout:
                            print(f"[Timeout] get_closed navegando a {detail_url}")
                            continue
                        except Error as e:
                            print(f"[Error] get_closed en {ref}: {str(e)}")
                            continue

                        ensure_not_cancelled(cancel_event)
                        html = page.content()
                        dates = self.extract_dates(html, current_date, is_closed=True)
                        closed_date = dates["resolved_date"]
                        closed_date = pd.to_datetime(
                            closed_date,
                            format="%d/%m/%y - %H:%M",
                            errors="coerce"       
                        )

                        if (closed_date >= last_date):
                            results.append(ref)
                        
                        pbar.update(1)
                        processed += 1
                        report_progress(
                            progress_cb, cancel_event,
                            processed,
                            progress_total,
                            f"Incidencias cerradas (Barra de progreso 1/2). Procesando {ref}... ({processed}/{total_refs or progress_total})",
                        )
                        sleep_with_cancel(cancel_event, 15)
            finally:
                context.close()
                browser.close()

        return results
        
    def scrape_details(
        self,
        last_date: date,
        period_date: date,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[Event] = None,
    ):
        ensure_not_cancelled(cancel_event)
        new_df: pd.DataFrame = self.load_df(TEMP_CSV)

        # Eliminamos el csv original nada más empezar el proceso para procurar
        # no coger uno previo.
        Path(TEMP_CSV).unlink(missing_ok=True)
        
        print("Realizando cálculos en los dataframes...")

        # Pasamos la fecha del último informe a un objeto datetime
        # y la fecha period_date ya que solo queremos centrarnos en
        # incidencias creadas en dicho período.
        last_date = pd.to_datetime(
            last_date.strftime("%d/%m/%y - %H:%M"),
            format="%d/%m/%y - %H:%M",
            errors="coerce"       
        )
        period_date = pd.to_datetime(
            period_date.strftime("%d/%m/%y - %H:%M"),
            format="%d/%m/%y - %H:%M",
            errors="coerce"       
        )
        current_date = datetime.now().strftime("%d/%m/%y - %H:%M")

        # Obtenemos la primera parte de los códigos de referencia que nos interesan 
        # (los que caben dentro del período y los que no están cerrados o resueltos)
        mask = (
            (new_df["Creada"] > last_date) | 
            (
                (~new_df["Estado"].isin(CLOSED_STATES_CSV)) & 
                (new_df["Creada"] >= period_date)
            )
        )
        ref_codes = new_df.loc[mask, "Reference"].to_list()

        # Necesitamos también las incidencias que se cerraron en el período que
        # estamos investigando actualmente, por lo que añadimos a new_df
        # las incidencias con esta casuística
        # 1) IDs que están cerrados en new_df y cuyas fechas de creación cuadran
        mask_closed = (
            (new_df["Creada"] >= period_date) & 
            (new_df["Creada"] <= last_date) &
            (new_df["Estado"].isin(CLOSED_STATES_CSV))
        )
        global_closed = set(new_df.loc[mask_closed, "Reference"].unique())
        # 2) Obtenemos los códigos de referencia de las cerradas en el período
        report_progress(progress_cb, cancel_event, 0, 1, "Calculando incidencias cerradas en el período…")
        period_closed = self.get_closed(cancel_event, progress_cb, 
                                        last_date, current_date, global_closed)
        # 3) Añadimos los códigos a la lista de códigos que necesitamos
        ref_codes.extend(period_closed)

        total_refs = len(ref_codes)
        progress_total = max(total_refs, 1)
        report_progress(progress_cb, cancel_event, 0, progress_total, "Iniciando scrapeo de los detalles…")

        print("Iniciando scrapeo de los detalles...")

        # Estructura de datos para las diferentes fechas de interés
        dates = {"reference": [], "response_date": [], "resolved_date": []}
        resolution_pauses = {"reference": [], "stop_date": [], "restart_date": [], "is_development": []}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                try:
                    self.authenticate(page)
                except PWTimeout as auth_error:
                    print(f"[Timeout] authenticate: {auth_error}")

                with tqdm(total=len(ref_codes), desc="Detalles scrapeados") as pbar:
                    processed = 0
                    for ref in ref_codes:
                        ensure_not_cancelled(cancel_event)
                        dates["reference"].append(ref)

                        response_date, resolved_date = current_date, current_date
                        html = ""

                        detail_url = f"{DETAIL_URL}{ref}"

                        def load_detail(retry: bool = False) -> bool:
                            nonlocal html
                            ensure_not_cancelled(cancel_event)
                            try:
                                page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
                                page.wait_for_selector("ul.vp-activity-list li", timeout=30_000)
                                html = page.content()
                                return True
                            except PWTimeout as load_error:
                                if not retry:
                                    print(f"[Timeout] {load_error}")
                                return False
                            except Error as e:
                                if not retry:
                                    print(f"[Error] scrape_details en {ref}: {str(e)}")
                                return False

                        if not load_detail():
                            # Reautenticamos una vez por si la sesión expiró y reintentamos.
                            try:
                                self.authenticate(page)
                                load_detail(retry=True)
                            except PWTimeout as retry_error:
                                print(f"[Timeout] retry authenticate: {retry_error}")

                        # Comprobamos si la incidencia está cerrada desde el csv
                        state_series = new_df.loc[new_df["Reference"] == ref, "Estado"]
                        is_closed = state_series.iloc[0] in CLOSED_STATES_CSV

                        result = self.extract_dates(html, current_date, is_closed)
                        response_str = result.get("response_date", current_date)
                        resolved_str = result.get("resolved_date", current_date)
                        pauses = result.get("resolution_pauses", {"stop_date": [], "restart_date": [], "is_development": []})

                        if response_str and response_str != current_date:
                            try:
                                response_date = datetime.strptime(response_str, "%d/%m/%y - %H:%M")
                            except ValueError:
                                pass
                        if resolved_str and resolved_str != current_date:
                            try:
                                resolved_date = datetime.strptime(resolved_str, "%d/%m/%y - %H:%M")
                            except ValueError:
                                pass

                        dates["response_date"].append(response_date)
                        dates["resolved_date"].append(resolved_date)

                        for stop_str, restart_str, is_dev in zip(pauses.get("stop_date", []), 
                                        pauses.get("restart_date", []), pauses.get("is_development", [])):
                            ensure_not_cancelled(cancel_event)
                            try:
                                stop_dt = datetime.strptime(stop_str, "%d/%m/%y - %H:%M")
                                restart_dt = datetime.strptime(restart_str, "%d/%m/%y - %H:%M")
                            except (TypeError, ValueError):
                                continue
                            resolution_pauses["reference"].append(ref)
                            resolution_pauses["stop_date"].append(stop_dt)
                            resolution_pauses["restart_date"].append(restart_dt)
                            resolution_pauses["is_development"].append(is_dev)

                        pbar.update(1)
                        processed += 1
                        report_progress(
                            progress_cb, cancel_event,
                            processed,
                            progress_total,
                            f"Detalles procesados (Barra de progreso 2/2). Procesando {ref}... ({processed}/{total_refs or progress_total})",
                        )
                        sleep_with_cancel(cancel_event, 15)
            finally:
                context.close()
                browser.close()

        # Obtenemos un nuevo dataframe con únicamente las incidencias
        # que nos interesan
        final_df = new_df[new_df["Reference"].isin(ref_codes)].copy()

        # Obtenemos un dataframe a partir de dates para procesamiento
        # posterior
        dates_df = pd.DataFrame({
            "Reference": dates["reference"],
            "Fecha Respuesta": dates["response_date"],
            "Fecha Resolucion": dates["resolved_date"],
        })

        # Procesamos ambos dataframes para obtener el final con las fechas
        final_df = final_df.merge(dates_df, on="Reference", how="left")

        # Creamos ahora el dataframe correspondiente para las pausas
        pause_rows = []
        for i in range(len(resolution_pauses["reference"])):
            ensure_not_cancelled(cancel_event)
            pause_rows.append(
                {
                    "Referencia": resolution_pauses["reference"][i],
                    "Fecha de Parada": resolution_pauses["stop_date"][i],
                    "Fecha de Reinicio": resolution_pauses["restart_date"][i],
                    "Es Desarrollo": resolution_pauses["is_development"][i]
                }
            )
        pauses_df = pd.DataFrame(pause_rows)

        def format_date_columns(df: pd.DataFrame, columns, current_date):
            """Convierte las columnas indicadas a string con el formato solicitado."""
            date_format = "%d/%m/%y - %H:%M"
            for column in columns:
                if column in df.columns:
                    series = df[column]
                    parsed = pd.to_datetime(series, format=date_format, errors="coerce")
                    formatted = parsed.dt.strftime(date_format)
                    df[column] = formatted.fillna(current_date)

        format_date_columns(
            final_df,
            ["Creada", "Fecha Respuesta", "Fecha Resolucion"],
            current_date
        )
        format_date_columns(
            pauses_df,
            ["Fecha de Parada", "Fecha de Reinicio"],
            current_date
        )

        def convert_numeric_columns_to_int(df: pd.DataFrame) -> None:
            numeric_columns = df.select_dtypes(include=["number"]).columns
            for column in numeric_columns:
                series = df[column]
                rounded = series.round()
                if rounded.isna().any():
                    df[column] = rounded.astype(pd.Int64Dtype())
                else:
                    df[column] = rounded.astype(int)

        convert_numeric_columns_to_int(final_df)
        report_progress(progress_cb, cancel_event, progress_total, progress_total, "Guardando resultados…")
        ensure_not_cancelled(cancel_event)

        # Reordenamos las columnas
        reordered_columns = ["Reference","Resumen","Tipo de solicitud del cliente",
            "Prioridad","Creada","Informador","Estado",
            "Tiempo hasta primera respuesta: Remaining Time",
            "Tiempo hasta resolución: Remaining Time",
            "Tiempo hasta primera respuesta: Goal (Contrato)",
            "Tiempo hasta resolución: Goal (Contrato)",
            "Fecha Respuesta", "Fecha Resolucion", 
            "Tiempo hasta primera respuesta: Goal",
            "Tiempo hasta resolución: Goal"
        ]
        final_df = final_df[reordered_columns]

        # Guardamos los nuevos csv
        final_df.to_csv(FINAL_CSV, index=False)  
        pauses_df.to_csv(PAUSES_CSV, index=False)

    def launch_scraping(
        self,
        last_date: date,
        period_date: date,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[Event] = None,
    ):
        self.scrape_csv(progress_cb, cancel_event=cancel_event)
        self.scrape_details(last_date, period_date, progress_cb, cancel_event=cancel_event)
