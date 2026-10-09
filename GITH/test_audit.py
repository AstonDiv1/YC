"""Regression tests run against temporary storage, never a live service."""
import os
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
        for url in ("/", "/conditions-utilisation", "/healthz", "/api/config", "/static/img/brand-mark.png"):
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
            self.assertIn("Écrire à YC Digital", html)
            self.assertNotIn('id="contactForm"', html)
            for url in ("/api/contact", "/api/submit"):
                self.assertEqual(self.client.post(url, json={}).status_code, 503)
            self.assertEqual(self.client.get(f"/{self.module.ADMIN_URL_SLUG}/connexion").status_code, 404)
        self.assertEqual(before, len(self.module.logic.list_contact_messages()))

    def test_missing_admin_password_disables_login(self):
        with patch.object(self.module, "_ADMIN_PASSWORD_HASH", None):
            self.assertEqual(self.client.get(f"/{self.module.ADMIN_URL_SLUG}/connexion").status_code, 404)
            self.assertEqual(self.client.get("/api/bookings").status_code, 401)


if __name__ == "__main__":
    unittest.main()
