import re
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import expect
from utilities import DEFAULT_TIMEOUT_MS, USERNAME, PASSWORD, LOGIN_URL, REPORT_URL


class Crawler:
    def __init__(self):
        pass

    def authenticate(self, page):
        page.goto(LOGIN_URL, wait_until="load", timeout=DEFAULT_TIMEOUT_MS)  # navega a la pantalla de login

        try:
            with page.expect_navigation(url=re.compile(r".*/servicedesk/customer/portal/81.*"), timeout=DEFAULT_TIMEOUT_MS):
                page.fill('input[id="login-form-username"]', USERNAME)
                page.fill('input[id="login-form-password"]', PASSWORD)
                page.click('input[id="login"]')
            page.wait_for_load_state("domcontentloaded")  # robustez tras el login
        except PWTimeout:
            # Atlassian puede redirigir a un formulario SSO alternativo; manejamos ese camino.
            try:
                page.wait_for_selector('input[id="os_username"]', timeout=DEFAULT_TIMEOUT_MS)
            except PWTimeout:
                # Si tampoco aparece el formulario alternativo, propagamos el timeout original.
                raise

            page.fill('input[id="os_username"]', USERNAME)
            page.fill('input[id="os_password"]', PASSWORD)
            with page.expect_navigation(timeout=DEFAULT_TIMEOUT_MS):
                page.click('button[id="js-login-submit"]')
            page.wait_for_load_state("domcontentloaded")

        if not page.url.startswith(REPORT_URL):
            page.goto(REPORT_URL, wait_until="domcontentloaded")

    def access_adv_report(self, page):
        # 1) Trigger del dropdown (el botón “Peticiones”)
        print(page.url)
        trigger = page.get_by_role("button", name="Peticiones")
        trigger.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        trigger.click()

        # 2) Esperar a que el contenedor del dropdown esté visible
        dropdown = page.locator("#requests-nav-dropdown")
        dropdown.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

        # 3) Pulsar el botón de peticiones avanzadas
        page.locator('a[id="adv-report"]').click()

        # 4) Esperar a que por lo menos un elemento tr esté visible
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
        expect(page.locator('thead th:has-text("Referencia")')).to_be_visible(timeout=DEFAULT_TIMEOUT_MS)
        expect(page.locator('tbody tr').first).to_be_visible(timeout=DEFAULT_TIMEOUT_MS)