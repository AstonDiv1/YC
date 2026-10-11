"""Regression tests run against temporary storage, never a live service."""
import os
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "YC_DIGITAL_DB_PATH": str(Path(cls.temp.name) / "bookings.db"),
            "YC_DIGITAL_UPLOAD_DIR": str(Path(cls.temp.name) / "uploads"),
            "ADMIN_PASSWORD": "test-only-password-not-for-production",
            "SECRET_KEY": "test-only-session-key",
            "CONTACT_EMAIL_ONLY": "0",
        })
        cls.env.start()
        import app
        cls.module = app
        app.app.config["TESTING"] = True
        cls.client = app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def test_public_pages_and_logo(self):
        for url in ("/", "/conditions-utilisation", "/healthz", "/api/config", "/static/img/numeryl-logo.png"):
            with self.subTest(url=url):
                with self.client.get(url) as response:
                    self.assertEqual(response.status_code, 200)

    def test_invalid_json_is_rejected(self):
        for url in ("/api/contact", "/api/submit"):
            for payload in ([1], "invalid", 42):
                with self.subTest(url=url, payload=payload):
                    self.assertEqual(self.client.post(url, json=payload).status_code, 400)
        self.assertEqual(self.client.post("/api/submit", json={"service": ["site_web"]}).status_code, 400)

    def test_contact_saved_without_sending_email(self):
        with patch.object(self.module.logic, "notify_new_message") as notify:
            response = self.client.post("/api/contact", json={
                "nom": "Test audit", "email": "test@example.com", "message": "Test local uniquement"
            }, environ_overrides={"REMOTE_ADDR": "192.0.2.20"})
        self.assertEqual(response.status_code, 200)
        notify.assert_called_once()
        self.assertTrue(any(m["id"] == response.json["message_id"] for m in self.module.logic.list_contact_messages()))

    def test_booking_preserves_availability(self):
        with patch.object(self.module.logic, "notify_new_booking"):
            response = self.client.post("/api/submit", json={
                "service": "site_web", "reponses": {"type_site": "Site vitrine"},
                "contact": {"nom": "Test audit", "email": "test@example.com", "disponibilite": "Après 18h"}
            }, environ_overrides={"REMOTE_ADDR": "192.0.2.21"})
        self.assertEqual(response.status_code, 200)
        booking = next(b for b in self.module.logic.list_bookings() if b["id"] == response.json["booking_id"])
        self.assertEqual(booking["contact"]["disponibilite"], "Après 18h")

    def test_estimates_match_services(self):
        logic = self.module.logic
        plain = logic.compute_recommendation("site_web", {"budget": "Moins de 300 €"})
        styles = logic.compute_recommendation("site_web", {"budget": "Moins de 300 €", "ambiance_visuelle": ["a", "b", "c", "d"]})
        self.assertEqual(plain, styles)
        self.assertIn("250 €", plain["fourchette_prix"])
        self.assertIn("450 €", logic.compute_recommendation("application", {})["fourchette_prix"])
        self.assertIn("100 €", logic.compute_recommendation("montage_pc", {})["fourchette_prix"])
        self.assertIn("Sur devis", logic.compute_recommendation("site_web", {"type_site": "E-commerce / boutique en ligne"})["fourchette_prix"])

    def test_admin_data_requires_authentication(self):
        self.assertEqual(self.client.get("/api/bookings").status_code, 401)
        self.assertEqual(self.client.get("/api/messages").status_code, 401)

    def test_email_mode_never_stores_requests(self):
        before = len(self.module.logic.list_contact_messages())
        with patch.object(self.module, "CONTACT_EMAIL_ONLY", True):
            html = self.client.get("/").get_data(as_text=True)
            self.assertIn('id="contactForm"', html)
            for url in ("/api/contact", "/api/submit"):
                self.assertEqual(self.client.post(url, json={}).status_code, 503)
            self.assertEqual(self.client.get(f"/{self.module.ADMIN_URL_SLUG}/connexion").status_code, 404)
        self.assertEqual(before, len(self.module.logic.list_contact_messages()))

    def test_missing_admin_password_disables_login(self):
        with patch.object(self.module, "_ADMIN_PASSWORD_HASH", None):
            self.assertEqual(self.client.get(f"/{self.module.ADMIN_URL_SLUG}/connexion").status_code, 404)
            self.assertEqual(self.client.get("/api/bookings").status_code, 401)

    def test_notification_failure_is_not_reported_as_success(self):
        with patch.object(self.module, "REQUIRE_EMAIL_DELIVERY", True), patch.object(self.module.logic, "email_notifications_configured", return_value=True), patch.object(self.module.logic, "notify_new_message", return_value=False):
            result = self.client.post("/api/contact", json={"nom":"Test", "email":"test@example.com", "message":"Test"}, environ_overrides={"REMOTE_ADDR":"192.0.2.30"})
        self.assertEqual(result.status_code, 503)

    def test_missing_notification_configuration_rejects_before_storage(self):
        before = len(self.module.logic.list_contact_messages())
        with patch.object(self.module, "REQUIRE_EMAIL_DELIVERY", True), patch.object(self.module.logic, "email_notifications_configured", return_value=False):
            self.assertEqual(self.client.post("/api/contact",json={"nom":"Test", "email":"test@example.com", "message":"Test"}).status_code,503)
        self.assertEqual(before,len(self.module.logic.list_contact_messages()))

    def test_booking_email_contains_answers_and_attachment(self):
        logic = self.module.logic
        with patch.dict(os.environ,{"RESEND_API_KEY":"test-only-key", "ADMIN_EMAIL":"test@example.com"}), patch.object(logic.resend.Emails,"send",return_value={"id":"mock-email"}) as sender, patch.object(self.module,"REQUIRE_EMAIL_DELIVERY",True):
            result = self.client.post("/api/submit",data={
                "service":"site_web", "reponses":'{"contexte":"Portfolio de menuiserie"}',
                "contact":'{"nom":"Test audit","email":"test@example.com","disponibilite":"Après 18h"}',
                "fichiers":(io.BytesIO(b"%PDF-1.4\nTest local"),"test.pdf")
            },content_type="multipart/form-data",environ_overrides={"REMOTE_ADDR":"192.0.2.31"})
        self.assertEqual(result.status_code,200)
        payload = sender.call_args.args[0]
        self.assertIn("Portfolio de menuiserie",payload["html"])
        self.assertIn("Portfolio de menuiserie",payload["text"])
        self.assertIn("Après 18h",payload["html"])
        self.assertEqual(payload["reply_to"],"test@example.com")
        self.assertEqual(payload["attachments"][0]["filename"],"test.pdf")

    def test_booking_notification_failure_is_not_reported_as_success(self):
        with patch.object(self.module,"REQUIRE_EMAIL_DELIVERY",True), patch.object(self.module.logic,"email_notifications_configured",return_value=True), patch.object(self.module.logic,"notify_new_booking",return_value=False):
            result = self.client.post("/api/submit",json={"service":"site_web","contact":{"nom":"Test", "email":"test@example.com"}},environ_overrides={"REMOTE_ADDR":"192.0.2.32"})
        self.assertEqual(result.status_code,503)

    def test_indexing_uses_public_origin_and_excludes_private_and_demo_pages(self):
        from xml.etree import ElementTree
        origin = "https://yc-digital.onrender.com"
        home = self.client.get("/", headers={"Host": "untrusted.example"}).get_data(as_text=True)
        self.assertIn('rel="canonical" href="' + origin + '/"', home)
        self.assertIn('"@type": "WebSite"', home)
        sitemap = self.client.get("/sitemap.xml", headers={"Host": "untrusted.example"})
        self.assertEqual(sitemap.status_code, 200)
        locations = ElementTree.fromstring(sitemap.data).findall(".//{*}loc")
        self.assertEqual([node.text for node in locations], [origin + "/"])
        robots = self.client.get("/robots.txt").get_data(as_text=True)
        self.assertIn("Sitemap: " + origin + "/sitemap.xml", robots)
        for path in ("/api/bookings", "/healthz", "/static/demos/artisan.html", "/not-found"):
            with self.client.get(path) as response:
                self.assertIn("noindex", response.headers["X-Robots-Tag"])

    def test_legal_page_reuses_brand_and_has_working_project_links(self):
        html = self.client.get("/conditions-utilisation").get_data(as_text=True)
        self.assertIn('src="/static/img/numeryl-logo.png"', html)
        self.assertIn('href="/#contact"', html)
        self.assertNotIn("ouvrirQuestionnaire", html)
        self.assertIn('name="robots" content="noindex,follow"', html)
        for asset in ("/static/css/site.css", "/static/css/legal.css"):
            with self.client.get(asset) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/css", response.content_type)

    def test_custom_domain_redirect_preserves_url_and_keeps_forms_and_health_available(self):
        with patch.object(self.module, "PUBLIC_SITE_URL", "https://numeryl.fr"), patch.object(self.module, "CANONICAL_REDIRECT_ENABLED", True):
            response = self.client.get("/conditions-utilisation?source=test", base_url="https://yc-digital.onrender.com")
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response.headers["Location"], "https://numeryl.fr/conditions-utilisation?source=test")
            self.assertEqual(self.client.head("/", base_url="https://www.numeryl.fr").status_code, 301)
            self.assertEqual(self.client.get("/", base_url="https://numeryl.fr").status_code, 200)
            for path in ("/healthz", "/api/config"):
                self.assertEqual(self.client.get(path, base_url="https://yc-digital.onrender.com").status_code, 200)
            self.assertEqual(self.client.post("/api/contact", json={}, base_url="https://yc-digital.onrender.com").status_code, 400)

    def test_mailbox_switch_updates_pages_and_failure_contact_together(self):
        with patch.object(self.module, "PUBLIC_CONTACT_EMAIL", "contact@numeryl.fr"):
            for path in ("/", "/conditions-utilisation"):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn("mailto:contact@numeryl.fr", html)
                self.assertNotIn("yc.digital33@gmail.com", html)
            with patch.object(self.module, "REQUIRE_EMAIL_DELIVERY", True), patch.object(self.module.logic, "email_notifications_configured", return_value=False):
                response = self.client.post("/api/contact", json={})
                self.assertEqual(response.status_code, 503)
                self.assertIn("contact@numeryl.fr", response.json["erreur"])


if __name__ == "__main__":
    unittest.main()
