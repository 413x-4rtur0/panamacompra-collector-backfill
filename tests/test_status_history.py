import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import common


class StatusHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = common.init_db(":memory:")
        self.conn.execute(
            """
            INSERT INTO opportunities (
                numero, grupo, estado, descripcion, short_description, entidad,
                dependencia, fecha, modalidad, link, first_seen, last_seen,
                date_folder, record_folder, index_json_path, detail_status,
                finish_date_guess, notified_at, last_notified_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "TEST-001", "Programadas", "Programada", "Test", "Test",
                "Entidad", "Dependencia", "2026-07-26", "Global", "https://example.test",
                "2026-07-26T00:00:00", "2026-07-26T00:00:00", "26-07-26",
                "/tmp/test", "/tmp/test.json", "saved", "", "2026-07-26T00:01:00", "Programada",
            ),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def row(group, status):
        return {
            "numero": "TEST-001",
            "grupo": group,
            "tipo_url": "solicitud-de-cotizacion",
            "estado": status,
            "descripcion": "Test",
            "short_description": "Test",
            "entidad": "Entidad",
            "dependencia": "Dependencia",
            "fecha": "2026-07-26",
            "modalidad": "Global",
            "link": "https://example.test",
            "finish_date_guess": "2026-07-27T17:00:00",
            "last_seen": "2026-07-26T00:02:00",
        }

    def test_programada_to_abierta_sets_flag_and_history(self):
        common.insert_or_update_index(
            self.conn, self.row("Abiertas", "Abierta"), source="test"
        )
        current = self.conn.execute(
            "SELECT grupo, estado, pending_status_change, status_flag, status_updated_at "
            "FROM opportunities WHERE numero = ?",
            ("TEST-001",),
        ).fetchone()
        history = self.conn.execute(
            "SELECT previous_estado, new_estado, closure_at, change_code, source, notification_state "
            "FROM opportunity_status_history WHERE numero = ?",
            ("TEST-001",),
        ).fetchone()
        self.assertEqual((current["grupo"], current["estado"], current["pending_status_change"],
                          current["status_flag"]),
                         ("Abiertas", "Abierta", "abierta", "abierta"))
        self.assertTrue(current["status_updated_at"])
        self.assertEqual(tuple(history),
                         ("Programada", "Abierta", "2026-07-27T17:00:00", "abierta", "test", "pending"))

    def test_abierta_to_cerrada_sets_closed_flag(self):
        self.conn.execute(
            "UPDATE opportunities SET grupo = 'Abiertas', estado = 'Abierta', last_notified_status = 'Abierta' "
            "WHERE numero = 'TEST-001'"
        )
        self.conn.commit()
        common.insert_or_update_index(
            self.conn, self.row("Closed", "Cerrada"), source="closed-index"
        )
        row = self.conn.execute(
            "SELECT h.change_code, o.pending_status_change, o.status_flag, o.closed_at, h.closure_at "
            "FROM opportunity_status_history h JOIN opportunities o USING (numero) "
            "WHERE h.numero = ? ORDER BY h.id DESC LIMIT 1",
            ("TEST-001",),
        ).fetchone()
        self.assertEqual((row["change_code"], row["pending_status_change"], row["status_flag"]),
                         ("cerrada", "cerrada", "cerrada"))
        self.assertTrue(row["closed_at"])
        self.assertEqual(row["closure_at"], "2026-07-27T17:00:00")

    def test_cancelled_status_gets_cancelled_code(self):
        self.assertEqual(
            common.status_change_code("Abierta", "Cancelada", "Abiertas", "Abiertas"),
            "cancelada",
        )

    def test_reconcile_legacy_snapshot_updates_old_record(self):
        self.conn.execute(
            "UPDATE opportunities SET grupo = 'Closed', estado = 'Cerrada', "
            "last_seen = '2026-07-26T00:03:00', last_notified_status = 'Abierta' "
            "WHERE numero = 'TEST-001'"
        )
        self.conn.commit()

        self.assertEqual(common.reconcile_legacy_status_history(self.conn), 1)
        row = self.conn.execute(
            "SELECT o.status_flag, o.closed_at, h.previous_estado, h.new_estado, "
            "h.source, h.notification_state, h.observed_at, h.closure_at "
            "FROM opportunities o JOIN opportunity_status_history h USING (numero) "
            "WHERE o.numero = ?",
            ("TEST-001",),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("cerrada", "2026-07-26T00:03:00", "Abierta", "Cerrada",
             "legacy-reconcile", "reconciled", "2026-07-26T00:03:00", None),
        )
        self.assertEqual(common.reconcile_legacy_status_history(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
