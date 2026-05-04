import asyncio, csv, io, json, time, zipfile
from threading import Event
from uuid import uuid4
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import date

from fastapi import FastAPI, Request, BackgroundTasks, Form, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from one_shot_crawler import OneShotCrawler
from utilities import INITIAL_CSV, FINAL_CSV, PAUSES_CSV

app = FastAPI(title="Proceso largo con SSE")
crawler = OneShotCrawler()

# Rutas de carpetas
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "web/templates"
STATIC_DIR = BASE_DIR / "web/static"
CSV_FILE = BASE_DIR / FINAL_CSV
INITIAL_PHOTOS_FILE = BASE_DIR / INITIAL_CSV
PAUSES_DATA_FILE = BASE_DIR / PAUSES_CSV
CSV_EXPORTS = [
    ("final_data.csv", CSV_FILE),
    ("initial_photos.csv", INITIAL_PHOTOS_FILE),
    ("paradaincidencias.csv", PAUSES_DATA_FILE),
]

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Estado en memoria
tasks: Dict[str, Dict[str, Any]] = {}
busy_lock = asyncio.Lock()
current_job_id: Optional[str] = None

@app.get("/")
async def index(request: Request):
    if busy_lock.locked() and current_job_id:
        return RedirectResponse(
            url=request.url_for("run_page", job_id=current_job_id),
            status_code=303,
        )
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "csv_exists": any(path.exists() for _, path in CSV_EXPORTS),
        },
    )

@app.post("/run")
async def post_run(
    request: Request,
    background: BackgroundTasks,
    last_date: date = Form(...),
    period_date: date = Form(...)
):
    async def schedule(job_id: str, last_date: date, period_date: date):
        # Corutina real en background
        async def _runner(job_id: str, last_date: date, period_date: date):
            global current_job_id
            state = tasks.get(job_id)
            if not state:
                return
            cancel_event: Event = state["cancel_event"]

            def mark_cancelled(message: str = "Tarea cancelada.") -> None:
                state["status"] = "cancelled"
                state["message"] = message
                state["progress"] = min(state.get("progress", 0) or 0, 99)

            try:
                async with busy_lock:
                    if cancel_event.is_set() or state.get("status") == "cancelling":
                        mark_cancelled()
                        return

                    current_job_id = job_id
                    state["status"] = "running"
                    state["progress"] = state.get("progress", 0) or 0
                    state["message"] = "Preparando datos…"

                    def progress_cb(done: int, total: int, message: str) -> None:
                        total = max(total, 1)
                        percent = int((done / total) * 100)
                        state["progress"] = max(0, min(percent, 100))
                        if message:
                            state["message"] = str(message)

                    try:
                        await asyncio.to_thread(
                            crawler.launch_scraping,
                            last_date,
                            period_date,
                            progress_cb=progress_cb,
                            cancel_event=cancel_event,
                        )
                    except asyncio.CancelledError as e:
                        mark_cancelled(e.args[0] if e.args else "Tarea cancelada.")
                        return

                    if cancel_event.is_set() or state.get("status") == "cancelling":
                        mark_cancelled()
                        return

                    state["status"] = "done"
                    state["progress"] = 100
                    state["message"] = "Proceso completado. CSV disponible."
            except asyncio.CancelledError:
                mark_cancelled(e.args[0] if e.args else "Tarea cancelada.")
            except Exception as e:
                s = tasks.get(job_id, {})
                s["status"] = "error"
                s["message"] = f"Error: {e!r}"
            finally:
                current_job_id = None
                state = tasks.get(job_id)
                if state:
                    state.pop("task", None)

        # Programa la tarea asíncrona
        task = asyncio.create_task(_runner(job_id, last_date, period_date))
        state = tasks.get(job_id)
        if state is not None:
            state["task"] = task

    # Si ya hay un job corriendo, redirige a su página
    if busy_lock.locked() and current_job_id:
        return RedirectResponse(
            url=request.url_for("run_page", job_id=current_job_id),
            status_code=303,
        )

    job_id = uuid4().hex
    cancel_event = Event()
    tasks[job_id] = {
        "status": "running",
        "progress": 0,
        "message": "Iniciado",
        "ts": time.time(),
        "cancel_event": cancel_event,
    }
    background.add_task(schedule, job_id, last_date, period_date)
    return RedirectResponse(url=request.url_for("run_page", job_id=job_id), status_code=303)

@app.get("/run/{job_id}", name="run_page")
async def run_page(request: Request, job_id: str):
    if job_id not in tasks:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("run.html", {"request": request, "job_id": job_id})

@app.post("/run/{job_id}/cancel")
async def cancel_run(job_id: str):
    state = tasks.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")

    status = state.get("status")
    if status in {"done", "error", "cancelled"}:
        return JSONResponse({"status": status})

    cancel_event: Optional[Event] = state.get("cancel_event")
    if cancel_event and not cancel_event.is_set():
        cancel_event.set()

    state["status"] = "cancelling"
    state["message"] = "Cancelando…"

    task = state.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()

    return JSONResponse({"status": "cancelling"})

@app.get("/events/{job_id}")
async def sse_events(job_id: str):
    # Generador SSE
    async def event_gen():
        while True:
            state = tasks.get(job_id)
            if not state:
                yield "event: error\n"
                yield 'data: {"error": "not_found"}\n\n'
                break

            payload = json.dumps({k: state.get(k) for k in ("status", "progress", "message")})
            yield f"data: {payload}\n\n"

            if state.get("status") in {"done", "error", "cancelled"}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )

@app.get("/download")
async def download_csv():
    available_files = [(name, path) for name, path in CSV_EXPORTS if path.exists()]

    if not available_files:
        return RedirectResponse(url="/", status_code=302)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for name, file_path in available_files:
            zip_file.write(file_path, arcname=name)

    zip_buffer.seek(0)
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="crawler_exports.zip"'},
    )
