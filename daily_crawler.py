from __future__ import annotations

import time, re
import pandas as pd

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout    
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from crawler import Crawler
from utilities import CURRENT_CODE_FILE, CSV_PATH, WANTED_COLS, DEFAULT_TIMEOUT_MS


def _td_best_text(td) -> str:
    """
    Extrae el texto 'humano' prioritizando:
    1) <a> con texto
    2) <span> con texto
    3) texto plano del td
    """
    a = td.find("a")
    if a and a.get_text(strip=True):
        return " ".join(a.get_text(" ", strip=True).split())
    span = td.find("span")
    if span and span.get_text(strip=True):
        return " ".join(span.get_text(" ", strip=True).split())
    return " ".join(td.get_text(" ", strip=True).split())

def _parse_created(value: str) -> str:
    """
    Intenta normalizar la fecha 'Creada' al formato '%d/%m/%Y - %H:%M'
    Probamos varios formatos habituales (con y sin año de 2 dígitos).
    """
    value = " ".join(value.split())
    candidates = [
        ("%d/%m/%y - %H:%M", "%d/%m/%Y - %H:%M"),  # p.ej. 09/09/25 - 09:31
        ("%d/%m/%Y - %H:%M", "%d/%m/%Y - %H:%M"),
        ("%d/%m/%Y %H:%M", "%d/%m/%Y - %H:%M"),
        ("%d/%m/%y %H:%M", "%d/%m/%Y - %H:%M"),
    ]
    for fin, fout in candidates:
        try:
            dt = datetime.strptime(value, fin)  # docs strptime. 
            return dt.strftime(fout)
        except ValueError:
            continue
    # Si nada cuadra, devolvemos el texto tal cual
    return value

def _read_last_code(txt_path: Path) -> int:
    """
    Lee del .txt un entero (XXXX de BENALMADEN-XXXX). Si no existe o no es válido, devuelve -1.
    """
    try:
        s = txt_path.read_text(encoding="utf-8").strip()
        return int(re.search(r"\d+", s).group()) if s else -1
    except Exception:
        return -1

def _write_last_code(txt_path: Path, value: int) -> None:
    txt_path.write_text(str(value), encoding="utf-8")

def _header_index_by_name(thead) -> Dict[str, int]:
    """
    Devuelve un dict {nombre_columna: índice_td} a partir del thead.
    Usa get_text(strip=True) que extrae correctamente el texto aun si hay <button> dentro del <th>.
    """
    names = [th.get_text(strip=True) for th in thead.select("th")]
    return {name: idx for idx, name in enumerate(names)}

class DailyCrawler(Crawler):
    def __init__(self):
        super().__init__()

    def generate_csv(
        self,
        html: str,
        txt_path: str | Path,
        csv_path: str | Path,
        wanted_cols: List[str] = WANTED_COLS,
        update_txt: bool = True,
        csv_sep: str = ",",
    ) -> pd.DataFrame:
        """
        - Lee el último BENALMADEN-XXXX de txt_path.
        - Extrae del HTML las columnas wanted_cols.
        - Filtra filas con XXXX > último.
        - Hace append al CSV (crea encabezados si no existe).
        - (Opcional) Actualiza txt con el nuevo máximo.
        - Devuelve el DataFrame de filas nuevas.
        """
        txt_path = Path(txt_path)
        csv_path = Path(csv_path)

        last_num = _read_last_code(txt_path)

        soup = BeautifulSoup(html, "lxml")
        thead = soup.select_one("thead")
        tbody = soup.select_one("tbody")
        if not thead or not tbody:
            raise ValueError("No se encontraron <thead> o <tbody> en el HTML.")

        name_to_idx = _header_index_by_name(thead)

        # Verifica que todas las columnas deseadas existen en el thead
        missing = [c for c in wanted_cols if c not in name_to_idx]
        if missing:
            raise ValueError(f"Faltan columnas en el thead: {missing}")

        rows_out = []
        max_num_seen: Optional[int] = None

        for tr in tbody.select("tr"):
            tds = tr.select("td")
            if not tds:
                continue

            # Texto de "Referencia" para filtrar por BENALMADEN-XXXX
            ref_idx = name_to_idx["Referencia"]
            if ref_idx >= len(tds):
                continue
            ref_text = _td_best_text(tds[ref_idx])
            m = re.search(r"BENALMADEN-(\d+)", ref_text, flags=re.I)  # regex para extraer XXXX
            if not m:
                continue
            num = int(m.group(1))
            if not (num > last_num):
                continue  # no supera el umbral; ignoramos

            # Construimos la fila solo con wanted_cols
            row_dict = {}
            for col in wanted_cols:
                idx = name_to_idx[col]
                if idx >= len(tds):
                    row_dict[col] = ""
                    continue
                cell_text = _td_best_text(tds[idx])
                if col == "Creada":
                    cell_text = _parse_created(cell_text)  # normalizamos fecha
                row_dict[col] = cell_text

            rows_out.append(row_dict)
            if max_num_seen is None or num > max_num_seen:
                max_num_seen = num

        df_new = pd.DataFrame(rows_out, columns=wanted_cols)

        # Append al CSV (sin índice). Si no existe el archivo, escribimos encabezados.
        write_header = not csv_path.exists()
        if not df_new.empty:
            df_new.to_csv(
                csv_path,
                mode="a",
                index=False,
                header=write_header,
                encoding="utf-8",
                sep=csv_sep,
                lineterminator="\n",
            )
            if update_txt and max_num_seen is not None and max_num_seen > last_num:
                _write_last_code(txt_path, max_num_seen)

        return df_new

    def scrape_html(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = (browser.new_context())
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page = context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)

            html = ""
            try:
                # 1) Login
                self.authenticate(page)

                # 2) Acceder a la página de incidencias
                self.access_adv_report(page)

                # 3) Captura y guarda el HTML final
                html = page.content()

            except PWTimeout as e:
                print(f"[Timeout] {e}")
            finally:
                context.close()
                browser.close()

                return html

if __name__ == "__main__":
    while True:
        print("------------------ NUEVA ITERACIÓN ------------------\n\n")
        daily_crawler = DailyCrawler()
        html = daily_crawler.scrape_html()
        # Path("scrap.html").write_text(html, encoding="utf-8")
        daily_crawler.generate_csv(html, CURRENT_CODE_FILE, CSV_PATH, WANTED_COLS)
        time.sleep(15)
