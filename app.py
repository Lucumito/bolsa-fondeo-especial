"""
Gestión y control automatizado de la Bolsa de Fondeo Especial
Prototipo funcional – Caso práctico Analista de Producto
Autor: Renato Suárez
"""
import sqlite3
import random
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date
from io import BytesIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ─────────────────────────── Parámetros de negocio ───────────────────────────
MONTO_BOLSA = 100_000_000          # USD, no revolvente
UMBRAL_ALERTA = 0.90               # alerta temprana
PRODUCTOS_ELEGIBLES = ["Factura Negociable", "Factoring Electrónico"]
BANCAS_ELEGIBLES = ["Corporativa", "Gran Empresa"]
PLAZO_MINIMO = 180                 # días

PRODUCTOS = PRODUCTOS_ELEGIBLES + ["Leasing", "Préstamo Comercial", "Carta Fianza"]
BANCAS = BANCAS_ELEGIBLES + ["Mediana Empresa", "Pequeña Empresa"]
EJECUTIVOS = ["Ana Torres", "Luis Ramírez", "Carla Mendoza", "Jorge Salazar", "Paola Ríos"]

DB_PATH = Path(__file__).parent / "bolsa_fondeo.db"
TZ = ZoneInfo("America/Lima")  # el servidor en la nube corre en UTC


def hora_lima():
    return datetime.now(TZ).replace(tzinfo=None)

# Directorio de correos (en producción vendría del Directorio Activo / CRM)
CORREOS_EJECUTIVOS = {
    "Ana Torres": "ana.torres@banco-demo.pe", "Luis Ramírez": "luis.ramirez@banco-demo.pe",
    "Carla Mendoza": "carla.mendoza@banco-demo.pe", "Jorge Salazar": "jorge.salazar@banco-demo.pe",
    "Paola Ríos": "paola.rios@banco-demo.pe",
}
CORREO_PRODUCTO = "producto.financiamiento@banco-demo.pe"
CORREOS_GERENCIA = ["gerencia.comercial@banco-demo.pe", "gerencia.finanzas@banco-demo.pe"]

st.set_page_config(page_title="Bolsa de Fondeo Especial", page_icon="💼", layout="wide")


# ─────────────────────────────── Base de datos ───────────────────────────────
def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS solicitudes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT UNIQUE,
                fecha TEXT,
                ejecutivo TEXT,
                banca TEXT,
                ruc TEXT,
                razon_social TEXT,
                producto TEXT,
                monto_solicitado REAL,
                monto_aprobado REAL DEFAULT 0,
                plazo INTEGER,
                tasa REAL,
                estado TEXT,
                motivo TEXT,
                fecha_resolucion TEXT,
                resuelto_por TEXT
            );
            CREATE TABLE IF NOT EXISTS auditoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, codigo TEXT, evento TEXT, detalle TEXT, usuario TEXT
            );
            CREATE TABLE IF NOT EXISTS alertas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, tipo TEXT, mensaje TEXT, destinatarios TEXT
            );
            CREATE TABLE IF NOT EXISTS correos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, codigo TEXT, evento TEXT, para TEXT, cc TEXT,
                asunto TEXT, cuerpo_html TEXT, estado TEXT DEFAULT 'Pendiente', detalle TEXT
            );
            """
        )
        if c.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0] == 0:
            seed(c)


def log(c, codigo, evento, detalle, usuario, fecha=None):
    c.execute(
        "INSERT INTO auditoria (fecha, codigo, evento, detalle, usuario) VALUES (?,?,?,?,?)",
        (fecha or hora_lima().strftime("%Y-%m-%d %H:%M:%S"), codigo, evento, detalle, usuario),
    )


def consumido(c):
    return c.execute(
        "SELECT COALESCE(SUM(monto_aprobado),0) FROM solicitudes WHERE estado='Aprobada'"
    ).fetchone()[0]


def siguiente_codigo(c):
    n = c.execute("SELECT COALESCE(MAX(id),0)+1 FROM solicitudes").fetchone()[0]
    return f"BF-{n:05d}"


# ──────────────────────────── Notificaciones por correo ───────────────────────
# Patrón "bandeja de salida": el correo se encola dentro de la misma transacción
# que la decisión y se envía después del commit. Si el envío falla, la decisión
# no se pierde y el correo queda registrado con su error para reintento.
def config_smtp():
    """Lee credenciales desde st.secrets['smtp']. Sin credenciales → modo simulado."""
    try:
        cfg = dict(st.secrets["smtp"])
        return cfg if cfg.get("usuario") and cfg.get("password") else None
    except Exception:
        return None


def encolar_correo(c, codigo, evento, para, cc, asunto, cuerpo_html):
    c.execute(
        "INSERT INTO correos (fecha, codigo, evento, para, cc, asunto, cuerpo_html) VALUES (?,?,?,?,?,?,?)",
        (hora_lima().strftime("%Y-%m-%d %H:%M:%S"), codigo, evento,
         "; ".join(para), "; ".join(cc), asunto, cuerpo_html),
    )


def despachar_correos():
    """Envía los correos pendientes. Devuelve la lista de correos procesados."""
    cfg = config_smtp()
    with conn() as c:
        pendientes = c.execute("SELECT * FROM correos WHERE estado='Pendiente' ORDER BY id").fetchall()
        procesados = []
        for m in pendientes:
            if not cfg:
                estado, detalle = "Simulado", "Sin credenciales SMTP: correo generado y registrado, no enviado."
            else:
                try:
                    para = [x for x in m["para"].split("; ") if x]
                    cc = [x for x in m["cc"].split("; ") if x]
                    destino_real = cfg.get("redirigir_a")  # modo demo: todo llega a un buzón propio
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = m["asunto"]
                    msg["From"] = cfg.get("remitente", cfg["usuario"])
                    msg["To"] = destino_real or ", ".join(para)
                    if cc and not destino_real:
                        msg["Cc"] = ", ".join(cc)
                    html = m["cuerpo_html"]
                    if destino_real:
                        html = (f"<p style='color:#888;font-size:12px'>[Demo] Destinatarios reales: "
                                f"Para: {escape(m['para'])} · CC: {escape(m['cc'])}</p>") + html
                    msg.attach(MIMEText(html, "html", "utf-8"))
                    destinos = [destino_real] if destino_real else para + cc
                    with smtplib.SMTP(cfg.get("host", "smtp.gmail.com"), int(cfg.get("puerto", 587)), timeout=15) as srv:
                        srv.starttls(context=ssl.create_default_context())
                        srv.login(cfg["usuario"], cfg["password"])
                        srv.sendmail(cfg["usuario"], destinos, msg.as_string())
                    estado, detalle = "Enviado", f"Entregado a {', '.join(destinos)}"
                except Exception as e:
                    estado, detalle = "Error", f"{type(e).__name__}: {e}"
            c.execute("UPDATE correos SET estado=?, detalle=? WHERE id=?", (estado, detalle, m["id"]))
            procesados.append({"para": m["para"], "asunto": m["asunto"], "estado": estado, "detalle": detalle})
    return procesados


def _plantilla(titulo, color, intro, filas, nota=""):
    filas_html = "".join(
        f"<tr><td style='padding:6px 12px;color:#555;border-bottom:1px solid #eee'>{escape(k)}</td>"
        f"<td style='padding:6px 12px;font-weight:600;border-bottom:1px solid #eee'>{escape(str(v))}</td></tr>"
        for k, v in filas
    )
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:620px;margin:auto;border:1px solid #e3e8ef;border-radius:8px;overflow:hidden;background:#ffffff;color:#222222">
      <div style="background:{color};color:#fff;padding:14px 18px;font-size:18px;font-weight:600">{escape(titulo)}</div>
      <div style="padding:16px 18px;color:#222">
        <p style="margin-top:0">{intro}</p>
        <table style="border-collapse:collapse;width:100%;font-size:14px">{filas_html}</table>
        {f'<p style="margin-top:14px">{nota}</p>' if nota else ''}
        <p style="color:#888;font-size:12px;margin-top:18px">Mensaje automático del sistema de control de la Bolsa de Fondeo Especial. No responder.</p>
      </div>
    </div>"""


def correo_solicitud(c, sol, saldo_restante):
    """Correo al ejecutivo con el resultado y toda la información de la operación."""
    color = {"Aprobada": "#1f7a4d", "Rechazada": "#b42318", "En revisión": "#c98a00"}[sol["estado"]]
    filas = [
        ("Código", sol["codigo"]), ("Estado", sol["estado"].upper()), ("Motivo", sol["motivo"]),
        ("Banca", sol["banca"]), ("RUC", sol["ruc"]), ("Razón social", sol["razon_social"]),
        ("Producto", sol["producto"]), ("Monto solicitado", f"USD {sol['monto_solicitado']:,.2f}"),
        ("Monto aprobado", f"USD {sol['monto_aprobado']:,.2f}"), ("Plazo", f"{sol['plazo']} días"),
        ("Tasa final ofrecida", f"{sol['tasa']:.2f}% TEA"),
        ("Saldo disponible de la bolsa", f"USD {saldo_restante:,.2f}"),
    ]
    nota = ("Su solicitud fue derivada al Ejecutivo de Producto, quien definirá una aprobación parcial o el rechazo."
            if sol["estado"] == "En revisión" else "")
    para = [CORREOS_EJECUTIVOS.get(sol["ejecutivo"], CORREO_PRODUCTO)]
    cc = [CORREO_PRODUCTO]
    encolar_correo(c, sol["codigo"], f"Solicitud {sol['estado'].lower()}", para, cc,
                   f"[Bolsa de Fondeo] {sol['codigo']} – {sol['estado']} – {sol['razon_social']}",
                   _plantilla(f"Solicitud {sol['codigo']}: {sol['estado']}", color,
                              f"Hola {escape(sol['ejecutivo'])}, este es el resultado de su solicitud de uso de bolsa:",
                              filas, nota))
    if sol["estado"] == "En revisión":
        encolar_correo(c, sol["codigo"], "Pendiente de revisión", [CORREO_PRODUCTO], [],
                       f"[Acción requerida] {sol['codigo']} excede el saldo disponible",
                       _plantilla("Solicitud pendiente de decisión", "#c98a00",
                                  "Una operación elegible excede el saldo disponible. Ingrese a la Bandeja de revisión "
                                  "para aprobarla parcialmente o rechazarla.", filas))


def fila_solicitud(c, codigo):
    return dict(c.execute("SELECT * FROM solicitudes WHERE codigo=?", (codigo,)).fetchone())


# ─────────────────────────── Motor de reglas ─────────────────────────────────
def validar_campos(d):
    """Completitud y formato: resuelve el problema de correos incompletos."""
    errores = []
    if not d["ejecutivo"]:
        errores.append("Seleccione el ejecutivo comercial.")
    if not d["ruc"].isdigit() or len(d["ruc"]) != 11:
        errores.append("El RUC debe tener 11 dígitos numéricos.")
    elif not d["ruc"].startswith(("10", "20")):
        errores.append("El RUC debe iniciar con 10 o 20.")
    if len(d["razon_social"].strip()) < 3:
        errores.append("Ingrese la razón social del cliente.")
    if d["monto"] <= 0:
        errores.append("El monto solicitado debe ser mayor a 0.")
    if d["plazo"] <= 0:
        errores.append("El plazo debe ser mayor a 0 días.")
    if not (0 < d["tasa"] < 30):
        errores.append("La tasa final debe estar entre 0% y 30%.")
    return errores


def evaluar_criterios(d):
    """Criterios de elegibilidad de la bolsa."""
    motivos = []
    if d["producto"] not in PRODUCTOS_ELEGIBLES:
        motivos.append("Producto no elegible")
    if d["banca"] not in BANCAS_ELEGIBLES:
        motivos.append("Banca no elegible")
    if d["plazo"] < PLAZO_MINIMO:
        motivos.append(f"Plazo menor a {PLAZO_MINIMO} días")
    return motivos


def procesar_solicitud(d, usuario="Sistema"):
    """Evalúa y resuelve automáticamente una solicitud. Devuelve (codigo, estado, motivo, monto_aprobado)."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")  # evita sobre-asignación por solicitudes simultáneas
        codigo = siguiente_codigo(c)
        ahora = hora_lima().strftime("%Y-%m-%d %H:%M:%S")
        uso_previo = consumido(c)
        saldo = MONTO_BOLSA - uso_previo
        motivos = evaluar_criterios(d)

        if motivos:
            estado, motivo, aprobado = "Rechazada", "; ".join(motivos), 0
        elif saldo <= 0:
            estado, motivo, aprobado = "Rechazada", "Bolsa agotada (100% utilizada)", 0
        elif d["monto"] > saldo:
            estado = "En revisión"
            motivo = f"Saldo insuficiente: disponible USD {saldo:,.0f}. Requiere decisión de Producto (aprobación parcial o rechazo)."
            aprobado = 0
        else:
            estado, motivo, aprobado = "Aprobada", "Cumple criterios y existe saldo disponible", d["monto"]

        resuelto = ahora if estado != "En revisión" else None
        c.execute(
            """INSERT INTO solicitudes (codigo, fecha, ejecutivo, banca, ruc, razon_social, producto,
               monto_solicitado, monto_aprobado, plazo, tasa, estado, motivo, fecha_resolucion, resuelto_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (codigo, ahora, d["ejecutivo"], d["banca"], d["ruc"], d["razon_social"].strip().upper(),
             d["producto"], d["monto"], aprobado, d["plazo"], d["tasa"], estado, motivo,
             resuelto, "Motor de reglas" if resuelto else None),
        )
        log(c, codigo, "Solicitud registrada", f"USD {d['monto']:,.0f} – {d['producto']}", d["ejecutivo"])
        log(c, codigo, estado, motivo, "Motor de reglas")
        uso_nuevo = consumido(c)
        correo_solicitud(c, fila_solicitud(c, codigo), MONTO_BOLSA - uso_nuevo)
        revisar_umbrales(c, uso_previo, uso_nuevo)
    despachar_correos()
    return codigo, estado, motivo, aprobado


def resolver_revision(codigo, decision, monto_parcial, usuario):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        uso_previo = consumido(c)
        saldo = MONTO_BOLSA - uso_previo
        ahora = hora_lima().strftime("%Y-%m-%d %H:%M:%S")
        if decision == "Aprobar parcial" and 0 < monto_parcial <= saldo:
            c.execute(
                "UPDATE solicitudes SET estado='Aprobada', monto_aprobado=?, motivo=?, fecha_resolucion=?, resuelto_por=? WHERE codigo=?",
                (monto_parcial, f"Aprobación parcial por saldo: USD {monto_parcial:,.0f}", ahora, usuario, codigo),
            )
            log(c, codigo, "Aprobada (parcial)", f"USD {monto_parcial:,.0f}", usuario)
        else:
            c.execute(
                "UPDATE solicitudes SET estado='Rechazada', motivo=?, fecha_resolucion=?, resuelto_por=? WHERE codigo=?",
                ("Saldo insuficiente – rechazada por Producto", ahora, usuario, codigo),
            )
            log(c, codigo, "Rechazada", "Saldo insuficiente", usuario)
        uso_nuevo = consumido(c)
        correo_solicitud(c, fila_solicitud(c, codigo), MONTO_BOLSA - uso_nuevo)
        revisar_umbrales(c, uso_previo, uso_nuevo)
    despachar_correos()


def revisar_umbrales(c, antes, despues):
    """Dispara alertas una sola vez al cruzar 90% y 100%."""
    for umbral, tipo, msg, dest, color in [
        (UMBRAL_ALERTA, "Alerta 90%",
         "La bolsa superó el 90% de utilización. Evaluar ampliación del monto o priorización de operaciones.",
         [CORREO_PRODUCTO] + CORREOS_GERENCIA, "#c98a00"),
        (1.0, "Bolsa agotada",
         "La bolsa llegó al 100%. Nuevas solicitudes se rechazarán automáticamente.",
         [CORREO_PRODUCTO] + CORREOS_GERENCIA + list(CORREOS_EJECUTIVOS.values()), "#b42318"),
    ]:
        if antes < umbral * MONTO_BOLSA <= despues:
            c.execute(
                "INSERT INTO alertas (fecha, tipo, mensaje, destinatarios) VALUES (?,?,?,?)",
                (hora_lima().strftime("%Y-%m-%d %H:%M:%S"), tipo, msg, "; ".join(dest)),
            )
            filas = [("Monto de la bolsa", f"USD {MONTO_BOLSA:,.0f}"),
                     ("Utilizado", f"USD {despues:,.0f} ({despues / MONTO_BOLSA:.1%})"),
                     ("Saldo disponible", f"USD {max(MONTO_BOLSA - despues, 0):,.0f}")]
            encolar_correo(c, None, tipo, dest, [], f"[Bolsa de Fondeo] {tipo}: utilización {despues / MONTO_BOLSA:.1%}",
                           _plantilla(tipo, color, escape(msg), filas))


# ────────────────────────────── Datos de ejemplo ─────────────────────────────
def seed(c):
    """Historial simulado jul–sep 2026 para que el tablero tenga contenido."""
    rnd = random.Random(42)
    # Empresas y RUCs ficticios (no corresponden a clientes reales)
    empresas = [
        ("20999000011", "AGROINDUSTRIAL LOS ANDES S.A."), ("20999000029", "DISTRIBUIDORA PACIFICO NORTE S.A.C."),
        ("20999000037", "METALURGICA SAN ISIDRO S.A."), ("20999000045", "BEBIDAS DEL VALLE S.A.A."),
        ("20999000053", "LACTEOS LA CAMPIÑA S.A."), ("20999000061", "CONSTRUCTORA HORIZONTE S.A.C."),
        ("20999000079", "RETAIL CENTRO LIMA S.A."), ("20999000087", "MAQUINARIAS DEL SUR S.A."),
        ("20999000095", "AVICOLA SANTA ROSA S.A."), ("20999000109", "AUTOMOTRIZ CORDILLERA S.A."),
        ("20999000117", "AGROEXPORT VILLA NORTE S.A.C."), ("20999000125", "TEXTIL ALPAQUERA DEL ALTIPLANO S.A.C."),
        ("20999000133", "LOGISTICA COSTA AZUL S.A.C."), ("20999000141", "PESQUERA BAHIA ESMERALDA S.A."),
    ]
    inicio = datetime(2026, 7, 1, 9, 0)
    uso = 0
    for i in range(1, 76):
        f = inicio + timedelta(days=rnd.randint(0, 93), hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        ruc, rs = rnd.choice(empresas)
        r = rnd.random()
        producto = rnd.choice(PRODUCTOS_ELEGIBLES) if r < 0.82 else rnd.choice(PRODUCTOS[2:])
        banca = rnd.choices(BANCAS, weights=[45, 35, 15, 5])[0]
        plazo = rnd.choices([90, 120, 150, 180, 240, 360], weights=[8, 12, 14, 30, 20, 16])[0]
        monto = round(rnd.choice([0.5, 0.8, 1, 1.5, 2, 2.5, 3]) * 1_000_000, -3)
        tasa = round(rnd.uniform(5.2, 8.9), 2)
        d = dict(producto=producto, banca=banca, plazo=plazo)
        motivos = evaluar_criterios(d)
        if motivos:
            estado, motivo, aprob = "Rechazada", "; ".join(motivos), 0
        elif uso + monto > MONTO_BOLSA * 0.86:
            continue
        else:
            estado, motivo, aprob = "Aprobada", "Cumple criterios y existe saldo disponible", monto
            uso += monto
        res = f + timedelta(minutes=rnd.randint(1, 5))
        c.execute(
            """INSERT INTO solicitudes (codigo, fecha, ejecutivo, banca, ruc, razon_social, producto,
               monto_solicitado, monto_aprobado, plazo, tasa, estado, motivo, fecha_resolucion, resuelto_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (None, f.strftime("%Y-%m-%d %H:%M:%S"), rnd.choice(EJECUTIVOS), banca, ruc, rs, producto,
             monto, aprob, plazo, tasa, estado, motivo, res.strftime("%Y-%m-%d %H:%M:%S"), "Motor de reglas"),
        )
    # Códigos correlativos por fecha
    rows = c.execute("SELECT id FROM solicitudes ORDER BY fecha").fetchall()
    for n, r in enumerate(rows, 1):
        c.execute("UPDATE solicitudes SET codigo=? WHERE id=?", (f"TMP-{n}", r["id"]))
    for n, r in enumerate(rows, 1):
        c.execute("UPDATE solicitudes SET codigo=? WHERE id=?", (f"BF-{n:05d}", r["id"]))
    for r in c.execute("SELECT codigo, fecha, fecha_resolucion, estado, motivo, ejecutivo, producto, monto_solicitado "
                       "FROM solicitudes ORDER BY fecha").fetchall():
        log(c, r["codigo"], "Solicitud registrada", f"USD {r['monto_solicitado']:,.0f} – {r['producto']}",
            r["ejecutivo"], r["fecha"])
        log(c, r["codigo"], r["estado"], r["motivo"], "Motor de reglas", r["fecha_resolucion"])


def df_solicitudes():
    with conn() as c:
        df = pd.read_sql("SELECT * FROM solicitudes ORDER BY fecha DESC", c)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["fecha_resolucion"] = pd.to_datetime(df["fecha_resolucion"])
    return df


def fmt_usd(x):
    return f"USD {x/1e6:,.2f} MM"


# ─────────────────────────────── Componentes UI ──────────────────────────────
def barra_bolsa(uso):
    pct = uso / MONTO_BOLSA
    color = "#1f7a4d" if pct < UMBRAL_ALERTA else ("#c98a00" if pct < 1 else "#b42318")
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=pct * 100,
        number={"suffix": "%", "valueformat": ".1f"},
        gauge={
            "axis": {"range": [0, 100]},
            "bar": {"color": color},
            "steps": [{"range": [0, 90], "color": "#eef2f6"}, {"range": [90, 100], "color": "#fdecc8"}],
            "threshold": {"line": {"color": "#b42318", "width": 3}, "value": 90},
        },
        title={"text": "Utilización de la bolsa"},
    ))
    fig.update_layout(height=240, margin=dict(l=20, r=20, t=50, b=10))
    return fig


def banner_estado(uso):
    pct = uso / MONTO_BOLSA
    if pct >= 1:
        st.error("🔴 **Bolsa agotada (100%).** Las nuevas solicitudes se rechazan automáticamente.")
    elif pct >= UMBRAL_ALERTA:
        st.warning(f"🟠 **Alerta: la bolsa está al {pct:.1%}.** Saldo disponible: {fmt_usd(MONTO_BOLSA-uso)}.")


def kpis(uso, df):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Monto de la bolsa", fmt_usd(MONTO_BOLSA))
    c2.metric("Utilizado", fmt_usd(uso), f"{uso/MONTO_BOLSA:.1%}", delta_color="off")
    c3.metric("Saldo disponible", fmt_usd(MONTO_BOLSA - uso))
    c4.metric("Solicitudes en revisión", int((df["estado"] == "En revisión").sum()))


# ─────────────────────────────────── Páginas ─────────────────────────────────
def pagina_nueva_solicitud():
    st.header("📝 Nueva solicitud de uso de bolsa")
    st.caption("Todos los campos son obligatorios. La respuesta es inmediata.")
    with conn() as c:
        uso = consumido(c)
    banner_estado(uso)
    st.info(f"Saldo disponible en este momento: **{fmt_usd(MONTO_BOLSA - uso)}**")

    with st.form("solicitud", clear_on_submit=False):
        a, b = st.columns(2)
        ejecutivo = a.selectbox("Ejecutivo comercial", EJECUTIVOS, index=None, placeholder="Seleccione el ejecutivo")
        banca = b.selectbox("Banca del cliente", BANCAS)
        ruc = a.text_input("RUC del cliente", max_chars=11, placeholder="20XXXXXXXXX")
        razon = b.text_input("Razón social")
        producto = a.selectbox("Producto", PRODUCTOS)
        monto = b.number_input("Monto solicitado (USD)", min_value=0.0, step=100_000.0, format="%.2f")
        plazo = a.number_input("Plazo (días)", min_value=0, step=30, value=180)
        tasa = b.number_input("Tasa final ofrecida al cliente (% TEA)", min_value=0.0, max_value=100.0, step=0.05, format="%.2f")
        enviado = st.form_submit_button("Enviar solicitud", type="primary", width="stretch")

    if enviado:
        d = dict(ejecutivo=ejecutivo, banca=banca, ruc=ruc.strip(), razon_social=razon, producto=producto,
                 monto=monto, plazo=int(plazo), tasa=tasa)
        errores = validar_campos(d)
        if errores:
            st.error("La solicitud no se registró. Corrija lo siguiente:\n\n- " + "\n- ".join(errores))
            return
        codigo, estado, motivo, aprobado = procesar_solicitud(d)
        if estado == "Aprobada":
            st.success(f"✅ **{codigo} APROBADA** por {fmt_usd(aprobado)}. {motivo}.")
            st.balloons()
        elif estado == "En revisión":
            st.warning(f"⏳ **{codigo} EN REVISIÓN.** {motivo}")
        else:
            st.error(f"❌ **{codigo} RECHAZADA.** Motivo: {motivo}.")
        with conn() as c:
            correos = c.execute("SELECT para, asunto, estado, cuerpo_html FROM correos WHERE codigo=? OR "
                                "(codigo IS NULL AND fecha >= ?) ORDER BY id",
                                (codigo, (hora_lima() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
        for m in correos:
            icono = {"Enviado": "📧", "Simulado": "✉️", "Error": "⚠️"}.get(m["estado"], "✉️")
            with st.expander(f"{icono} Correo {m['estado'].lower()} → {m['para']}"):
                st.caption(m["asunto"])
                st.html(m["cuerpo_html"])
        st.caption("Puede consultar el estado en cualquier momento en «Consultar estado».")


def pagina_consulta():
    st.header("🔎 Consultar estado de solicitudes")
    df = df_solicitudes()
    a, b = st.columns([1, 2])
    ejecutivo = a.selectbox("Ejecutivo", ["Todos"] + EJECUTIVOS)
    buscar = b.text_input("Buscar por código, RUC o razón social")
    v = df.copy()
    if ejecutivo != "Todos":
        v = v[v["ejecutivo"] == ejecutivo]
    if buscar:
        q = buscar.upper()
        v = v[v["codigo"].str.upper().str.contains(q) | v["ruc"].str.contains(q) | v["razon_social"].str.contains(q)]
    st.dataframe(
        v[["codigo", "fecha", "ejecutivo", "razon_social", "producto", "monto_solicitado", "monto_aprobado",
           "plazo", "estado", "motivo"]],
        hide_index=True, width="stretch",
        column_config={
            "fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm"),
            "monto_solicitado": st.column_config.NumberColumn("Solicitado (USD)", format="%,.0f"),
            "monto_aprobado": st.column_config.NumberColumn("Aprobado (USD)", format="%,.0f"),
            "plazo": st.column_config.NumberColumn("Plazo (d)"),
            "razon_social": "Razón social", "codigo": "Código", "ejecutivo": "Ejecutivo",
            "producto": "Producto", "estado": "Estado", "motivo": "Motivo",
        },
    )


def pagina_dashboard():
    st.header("📊 Tablero de control de la bolsa")
    df = df_solicitudes()
    with conn() as c:
        uso = consumido(c)
    banner_estado(uso)
    kpis(uso, df)

    a, b = st.columns([1, 2])
    a.plotly_chart(barra_bolsa(uso), width="stretch")

    # Consumo acumulado + proyección de agotamiento
    ap = df[df["estado"] == "Aprobada"].sort_values("fecha_resolucion")
    if not ap.empty:
        serie = ap.groupby(ap["fecha_resolucion"].dt.date)["monto_aprobado"].sum().cumsum()
        dias = max((serie.index[-1] - serie.index[0]).days, 1)
        ritmo = serie.iloc[-1] / dias
        saldo = MONTO_BOLSA - uso
        fecha_agot = serie.index[-1] + timedelta(days=int(saldo / ritmo)) if ritmo > 0 and saldo > 0 else None
        fig = go.Figure()
        fig.add_scatter(x=list(serie.index), y=serie.values / 1e6, mode="lines", name="Consumo acumulado",
                        line=dict(color="#1d4e89", width=3), fill="tozeroy", fillcolor="rgba(29,78,137,0.08)")
        if fecha_agot:
            fig.add_scatter(x=[serie.index[-1], fecha_agot], y=[serie.iloc[-1] / 1e6, MONTO_BOLSA / 1e6],
                            mode="lines", name="Proyección", line=dict(color="#c98a00", dash="dash"))
        fig.add_hline(y=MONTO_BOLSA / 1e6, line_color="#b42318", annotation_text="Límite 100 MM")
        fig.add_hline(y=MONTO_BOLSA * UMBRAL_ALERTA / 1e6, line_color="#c98a00", line_dash="dot",
                      annotation_text="Alerta 90%")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="USD MM",
                          legend=dict(orientation="h", y=-0.2), title="Consumo acumulado y proyección")
        b.plotly_chart(fig, width="stretch")
        if fecha_agot:
            st.caption(f"Ritmo promedio de consumo: **{fmt_usd(ritmo * 30)} por mes**. "
                       f"Al ritmo actual la bolsa se agotaría alrededor del **{fecha_agot:%d/%m/%Y}**.")

    c1, c2, c3 = st.columns(3)
    por_banca = ap.groupby("banca")["monto_aprobado"].sum().reset_index()
    c1.plotly_chart(px.pie(por_banca, names="banca", values="monto_aprobado", hole=0.55,
                           title="Uso por banca", color_discrete_sequence=["#1d4e89", "#6fa8dc"])
                    .update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10)), width="stretch")
    por_prod = ap.groupby("producto")["monto_aprobado"].sum().reset_index()
    c2.plotly_chart(px.bar(por_prod, x="producto", y="monto_aprobado", title="Uso por producto",
                           color_discrete_sequence=["#1d4e89"])
                    .update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10), xaxis_title="", yaxis_title="USD"),
                    width="stretch")
    por_ej = ap.groupby("ejecutivo")["monto_aprobado"].sum().sort_values().reset_index()
    c3.plotly_chart(px.bar(por_ej, y="ejecutivo", x="monto_aprobado", orientation="h", title="Uso por ejecutivo",
                           color_discrete_sequence=["#1d4e89"])
                    .update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10), xaxis_title="USD", yaxis_title=""),
                    width="stretch")

    st.subheader("Eficiencia del proceso")
    resueltas = df.dropna(subset=["fecha_resolucion"])
    t = (resueltas["fecha_resolucion"] - resueltas["fecha"]).dt.total_seconds().mean() / 60
    tasa_aprob = (df["estado"] == "Aprobada").mean()
    tasa_ap = ap["tasa"].mean() if not ap.empty else 0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Solicitudes totales", len(df))
    m2.metric("Tasa de aprobación", f"{tasa_aprob:.0%}")
    m3.metric("Tiempo medio de respuesta", f"{t:.1f} min", "antes: horas/días", delta_color="off")
    m4.metric("Tasa promedio ofrecida (aprobadas)", f"{tasa_ap:.2f}%")


def pagina_bandeja():
    st.header("📥 Bandeja de Producto – solicitudes en revisión")
    st.caption("Solo llegan aquí las excepciones: operaciones que cumplen criterios pero exceden el saldo disponible.")
    df = df_solicitudes()
    pend = df[df["estado"] == "En revisión"]
    with conn() as c:
        saldo = MONTO_BOLSA - consumido(c)
    st.info(f"Saldo disponible: **{fmt_usd(saldo)}**")
    if pend.empty:
        st.success("No hay solicitudes pendientes. El motor de reglas resolvió todo automáticamente.")
        return
    for _, r in pend.iterrows():
        with st.expander(f"{r['codigo']} · {r['razon_social']} · USD {r['monto_solicitado']:,.0f}", expanded=True):
            st.write(f"**Ejecutivo:** {r['ejecutivo']} · **Producto:** {r['producto']} · **Banca:** {r['banca']} · "
                     f"**Plazo:** {r['plazo']} d · **Tasa:** {r['tasa']:.2f}%")
            a, b, c = st.columns([2, 2, 1])
            decision = a.radio("Decisión", ["Aprobar parcial", "Rechazar"], key=f"d{r['codigo']}", horizontal=True)
            monto = b.number_input("Monto a aprobar (USD)", min_value=0.0, max_value=float(max(saldo, 0)),
                                   value=float(max(saldo, 0)), step=100_000.0, key=f"m{r['codigo']}")
            if c.button("Confirmar", key=f"b{r['codigo']}", type="primary"):
                if decision == "Aprobar parcial" and not (0 < monto <= saldo):
                    st.error("No hay saldo para aprobar ese monto. Ingrese un monto válido o rechace la solicitud.")
                else:
                    resolver_revision(r["codigo"], decision, monto, "Ejecutivo de Producto")
                    st.rerun()


def pagina_analisis():
    st.header("🧭 Análisis para la toma de decisiones")
    st.caption("Apoya las dos decisiones del caso: ampliar el monto de la bolsa y flexibilizar los criterios de uso.")
    df = df_solicitudes()
    rech = df[df["estado"] == "Rechazada"].copy()
    with conn() as c:
        uso = consumido(c)

    st.subheader("1. ¿Por qué se rechazan las solicitudes?")
    motivos = rech.assign(m=rech["motivo"].str.split("; ")).explode("m")
    resumen = motivos.groupby("m").agg(solicitudes=("codigo", "count"), monto=("monto_solicitado", "sum")) \
        .sort_values("monto", ascending=True).reset_index()
    st.plotly_chart(px.bar(resumen, y="m", x="monto", orientation="h", text="solicitudes",
                           title="Demanda rechazada por motivo (USD; etiqueta = n° de solicitudes)",
                           color_discrete_sequence=["#b42318"])
                    .update_layout(height=280, margin=dict(l=10, r=10, t=40, b=10), yaxis_title="", xaxis_title="USD"),
                    width="stretch")

    st.subheader("2. Simulador de flexibilización de criterios")
    a, b, c = st.columns(3)
    plazo_min = a.slider("Plazo mínimo (días)", 60, 180, PLAZO_MINIMO, 30)
    incluir_mediana = b.checkbox("Incluir Banca Mediana Empresa")
    monto_bolsa = c.number_input("Monto de bolsa (USD MM)", 50, 300, MONTO_BOLSA // 1_000_000, 10) * 1_000_000

    bancas = BANCAS_ELEGIBLES + (["Mediana Empresa"] if incluir_mediana else [])
    elegibles_nuevas = rech[
        rech["producto"].isin(PRODUCTOS_ELEGIBLES) & rech["banca"].isin(bancas) & (rech["plazo"] >= plazo_min)
    ]
    demanda_extra = elegibles_nuevas["monto_solicitado"].sum()
    uso_sim = uso + demanda_extra
    s1, s2, s3 = st.columns(3)
    s1.metric("Operaciones adicionales que calificarían", len(elegibles_nuevas))
    s2.metric("Demanda adicional", fmt_usd(demanda_extra))
    s3.metric("Utilización simulada", f"{uso_sim / monto_bolsa:.1%}",
              f"{(uso_sim / monto_bolsa) - (uso / MONTO_BOLSA):+.1%} vs. actual")
    if uso_sim > monto_bolsa:
        st.error(f"Con estos criterios la demanda supera la bolsa en {fmt_usd(uso_sim - monto_bolsa)}: "
                 "flexibilizar exige también ampliar el monto.")
    elif uso_sim / monto_bolsa >= UMBRAL_ALERTA:
        st.warning("Con estos criterios la bolsa entraría en zona de alerta (≥90%).")
    else:
        st.success("Hay espacio para flexibilizar sin superar el umbral de alerta.")

    st.subheader("3. Tasa ofrecida por producto (operaciones aprobadas)")
    ap = df[df["estado"] == "Aprobada"]
    st.plotly_chart(px.box(ap, x="producto", y="tasa", color="banca", points="all",
                           color_discrete_sequence=["#1d4e89", "#6fa8dc"])
                    .update_layout(height=320, margin=dict(l=10, r=10, t=20, b=10), yaxis_title="Tasa % TEA", xaxis_title=""),
                    width="stretch")


def pagina_trazabilidad():
    st.header("🗂️ Trazabilidad y reportes")
    with conn() as c:
        alertas = pd.read_sql("SELECT fecha, tipo, mensaje, destinatarios FROM alertas ORDER BY id DESC", c)
        audit = pd.read_sql("SELECT fecha, codigo, evento, detalle, usuario FROM auditoria ORDER BY fecha DESC, id DESC", c)
    st.subheader("Alertas emitidas")
    if alertas.empty:
        st.caption("Aún no se ha cruzado el 90% ni el 100%. Las alertas se registran (y notificarían por correo) "
                   "automáticamente al cruzar cada umbral.")
    else:
        st.dataframe(alertas, hide_index=True, width="stretch")
    st.subheader("Correos automáticos")
    with conn() as c:
        correos = pd.read_sql("SELECT id, fecha, codigo, evento, para, asunto, estado, detalle, cuerpo_html "
                              "FROM correos ORDER BY id DESC", c)
    if correos.empty:
        st.caption("Aún no se han generado correos.")
    else:
        st.dataframe(correos.drop(columns=["cuerpo_html", "id"]), hide_index=True, width="stretch", height=220)
        sel = st.selectbox("Vista previa", correos["id"],
                           format_func=lambda i: correos.set_index("id").loc[i, "asunto"])
        st.html(correos.set_index("id").loc[sel, "cuerpo_html"])
    st.subheader("Bitácora de auditoría")
    st.dataframe(audit, hide_index=True, width="stretch", height=320)

    df = df_solicitudes()
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Solicitudes", index=False)
        audit.to_excel(w, sheet_name="Auditoria", index=False)
        alertas.to_excel(w, sheet_name="Alertas", index=False)
    st.download_button("⬇️ Descargar reporte (Excel)", buf.getvalue(),
                       file_name=f"reporte_bolsa_fondeo_{hora_lima():%Y%m%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def pagina_admin():
    st.header("⚙️ Demo")
    st.caption("Herramientas para la presentación del prototipo.")
    with conn() as c:
        uso = consumido(c)
    st.write(f"Utilización actual: **{uso / MONTO_BOLSA:.1%}**")
    cfg = config_smtp()
    if cfg:
        st.success(f"📧 Envío de correos activo desde {cfg.get('remitente', cfg['usuario'])}"
                   + (f" (modo demo: todo se redirige a {cfg['redirigir_a']})" if cfg.get("redirigir_a") else ""))
        if st.button("Enviar correo de prueba"):
            with conn() as c:
                encolar_correo(c, None, "Prueba", [cfg.get("redirigir_a") or cfg["usuario"]], [],
                               "[Bolsa de Fondeo] Correo de prueba",
                               _plantilla("Correo de prueba", "#1d4e89", "La configuración SMTP funciona correctamente.",
                                          [("Fecha", hora_lima().strftime("%d/%m/%Y %H:%M"))]))
            r = despachar_correos()
            (st.success if r and r[0]["estado"] == "Enviado" else st.error)(r[0]["detalle"] if r else "Sin correos")
    else:
        st.info("✉️ Correos en modo simulado: se generan y registran, pero no se envían. "
                "Configure `.streamlit/secrets.toml` para enviarlos de verdad (ver README).")
    if st.button("Llevar la bolsa al ~89% (para demostrar la alerta del 90%)"):
        objetivo = MONTO_BOLSA * 0.89 - uso
        if objetivo > 0:
            procesar_solicitud(dict(ejecutivo="Ana Torres", banca="Corporativa", ruc="20999000011",
                                    razon_social="AGROINDUSTRIAL LOS ANDES S.A.", producto="Factoring Electrónico",
                                    monto=round(objetivo, -3), plazo=240, tasa=6.5))
        st.rerun()
    if st.button("Reiniciar datos de ejemplo", type="secondary"):
        DB_PATH.unlink(missing_ok=True)
        init_db()
        st.rerun()


# ──────────────────────────────────── Main ───────────────────────────────────
def main():
    init_db()
    st.sidebar.title("💼 Bolsa de Fondeo Especial")
    st.sidebar.caption("Financiamiento de Ventas · USD 100 MM · no revolvente")
    st.sidebar.caption("⚠️ Prototipo con datos simulados: clientes, RUCs, ejecutivos y operaciones son ficticios.")
    rol = st.sidebar.radio("Perfil", ["Ejecutivo Comercial", "Ejecutivo de Producto"])
    if rol == "Ejecutivo Comercial":
        paginas = {"Nueva solicitud": pagina_nueva_solicitud, "Consultar estado": pagina_consulta,
                   "Disponibilidad de la bolsa": pagina_dashboard}
    else:
        paginas = {"Tablero de control": pagina_dashboard, "Bandeja de revisión": pagina_bandeja,
                   "Análisis y simulador": pagina_analisis, "Trazabilidad y reportes": pagina_trazabilidad,
                   "Consultar solicitudes": pagina_consulta, "Demo": pagina_admin}
    pagina = st.sidebar.radio("Menú", list(paginas))
    with conn() as c:
        uso = consumido(c)
    st.sidebar.progress(min(uso / MONTO_BOLSA, 1.0), text=f"Utilizado {uso / MONTO_BOLSA:.1%}")
    st.sidebar.markdown(
        f"**Criterios vigentes**\n- Productos: {', '.join(PRODUCTOS_ELEGIBLES)}\n"
        f"- Banca: {', '.join(BANCAS_ELEGIBLES)}\n- Plazo ≥ {PLAZO_MINIMO} días"
    )
    paginas[pagina]()


if __name__ == "__main__":
    main()
