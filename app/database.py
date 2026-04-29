import sqlite3
import os
import json
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "pagoflow.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre     TEXT NOT NULL,
            descripcion TEXT,
            creado_en  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS lotes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER NOT NULL,
            numero          TEXT NOT NULL,
            cliente_nombre  TEXT NOT NULL,
            cliente_phone   TEXT UNIQUE NOT NULL,
            monto_total     REAL DEFAULT 0,
            monto_pagado    REAL DEFAULT 0,
            contrato_json   TEXT,
            contrato_archivo TEXT,
            creado_en       TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS pagos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            lote_id      INTEGER NOT NULL,
            monto        REAL,
            monto_esperado REAL,
            referencia   TEXT,
            banco        TEXT,
            fecha        TEXT,
            archivo_drive TEXT,
            drive_ok     INTEGER DEFAULT 0,
            discrepancia TEXT,
            estatus      TEXT DEFAULT 'ok',
            registrado   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lote_id) REFERENCES lotes(id)
        );
    """)
    conn.commit()
    conn.close()
    print("✅ Base de datos lista")


# ─── Proyectos ────────────────────────────────────────────────────────────────

def create_project(nombre: str, descripcion: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO projects (nombre, descripcion) VALUES (?, ?)",
        (nombre, descripcion)
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid

def get_projects() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM projects ORDER BY creado_en DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_project(pid: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── Lotes ────────────────────────────────────────────────────────────────────

def create_lote(project_id, numero, cliente_nombre, cliente_phone) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO lotes (project_id, numero, cliente_nombre, cliente_phone)
           VALUES (?, ?, ?, ?)""",
        (project_id, numero, cliente_nombre, cliente_phone)
    )
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid

def get_lote_by_phone(phone: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM lotes WHERE cliente_phone=?", (phone,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_lotes_by_project(project_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM lotes WHERE project_id=? ORDER BY numero",
        (project_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_lote(lote_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_lote_contract(lote_id: int, contrato_json: dict, archivo: str, monto_total: float):
    conn = get_conn()
    conn.execute(
        """UPDATE lotes SET contrato_json=?, contrato_archivo=?, monto_total=?
           WHERE id=?""",
        (json.dumps(contrato_json, ensure_ascii=False), archivo, monto_total, lote_id)
    )
    conn.commit()
    conn.close()

def update_monto_pagado(lote_id: int):
    conn = get_conn()
    total = conn.execute(
        "SELECT COALESCE(SUM(monto),0) FROM pagos WHERE lote_id=?", (lote_id,)
    ).fetchone()[0]
    conn.execute("UPDATE lotes SET monto_pagado=? WHERE id=?", (total, lote_id))
    conn.commit()
    conn.close()

def add_lote_client(project_id, numero, nombre, phone):
    conn = get_conn()
    conn.execute(
        """INSERT INTO lotes (project_id, numero, cliente_nombre, cliente_phone)
           VALUES (?,?,?,?)
           ON CONFLICT(cliente_phone) DO UPDATE SET
             numero=excluded.numero, cliente_nombre=excluded.cliente_nombre""",
        (project_id, numero, nombre, phone)
    )
    conn.commit()
    conn.close()


def update_lote_info(lid, numero, nombre, phone):
    conn = get_conn()
    conn.execute(
        "UPDATE lotes SET numero=?, cliente_nombre=?, cliente_phone=? WHERE id=?",
        (numero, nombre, phone, lid)
    )
    conn.commit()
    conn.close()


# ─── Pagos ────────────────────────────────────────────────────────────────────

def save_pago(lote_id, monto, monto_esperado, referencia, banco,
              fecha, archivo_drive, drive_ok, discrepancia, estatus):
    conn = get_conn()
    conn.execute(
        """INSERT INTO pagos
           (lote_id, monto, monto_esperado, referencia, banco,
            fecha, archivo_drive, drive_ok, discrepancia, estatus)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (lote_id, monto, monto_esperado, referencia, banco,
         fecha, archivo_drive, int(drive_ok), discrepancia, estatus)
    )
    conn.commit()
    conn.close()
    update_monto_pagado(lote_id)

def get_pagos_by_lote(lote_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM pagos WHERE lote_id=? ORDER BY registrado DESC",
        (lote_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_pagos() -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.*, l.numero as lote_numero, l.cliente_nombre, l.monto_total,
               pr.nombre as proyecto_nombre
        FROM pagos p
        JOIN lotes l ON p.lote_id = l.id
        JOIN projects pr ON l.project_id = pr.id
        ORDER BY p.registrado DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    conn = get_conn()
    total_cobrado  = conn.execute("SELECT COALESCE(SUM(monto),0) FROM pagos").fetchone()[0]
    total_pagos    = conn.execute("SELECT COUNT(*) FROM pagos").fetchone()[0]
    lotes_activos  = conn.execute("SELECT COUNT(*) FROM lotes").fetchone()[0]
    discrepancias  = conn.execute("SELECT COUNT(*) FROM pagos WHERE estatus != 'ok'").fetchone()[0]
    por_cobrar     = conn.execute(
        "SELECT COALESCE(SUM(monto_total - monto_pagado),0) FROM lotes WHERE monto_total > 0"
    ).fetchone()[0]
    conn.close()
    return {
        "total_cobrado":  round(total_cobrado, 2),
        "total_pagos":    total_pagos,
        "lotes_activos":  lotes_activos,
        "discrepancias":  discrepancias,
        "por_cobrar":     round(por_cobrar, 2),
    }
